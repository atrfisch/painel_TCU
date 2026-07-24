#!/usr/bin/env python3
"""
coleta.py — processos do TCU que têm o MPO (ou suas secretarias) como
unidade jurisdicionada.

DUAS FONTES, uma verificada e outra a configurar:

  1. BTCU (Boletim do TCU)  — VERIFICADA E FUNCIONANDO.
     PDFs públicos em sessoes-portal-ms.apps.tcu.gov.br. Cada edição declara
     "Unidade jurisdicionada:" por processo. Dá: número, relator, colegiado,
     assunto, natureza e movimentações (a seção do boletim indica a fase).
     NÃO dá o campo "Estado" (Aberto/Encerrado) — o boletim não publica isso.

  2. Pesquisa Integrada — A CONFIGURAR (veja PESQUISA_* abaixo).
     É a fonte que tem o filtro "Estado: Aberto". A API não é documentada;
     capture-a no navegador (F12 > Network) e preencha as constantes.
     Enquanto não estiver configurada, o painel indica isso explicitamente
     em vez de fingir que a lista está completa.

Uso:
    python coleta.py --saida site/dados.json
    python coleta.py --saida site/dados.json --desde-id 22110 --max-edicoes 1500
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("coleta")

# =========================================================================== #
# CONFIGURAÇÃO
# =========================================================================== #

BTCU_URL = "https://sessoes-portal-ms.apps.tcu.gov.br/api/sessoes/downloadPautaPublicada/{id}"

# Ids observados: 22110≈ago/2024, 22495≈abr/2025, 23186≈mar/2026, 23240≈mai/2026.
# Cerca de 1 id/dia, com lacunas (o id é compartilhado com outros documentos).
BTCU_ANCORA = 23240
BTCU_DATA_ANCORA = date(2026, 5, 15)
BTCU_MISSES = 45

# --- Pesquisa Integrada: endpoint público de processos (capturado no navegador)
# Traz o campo Estado (Aberto/Encerrado) e a lista completa, não só o que passou
# pelo boletim. É paginado: ?inicio= avança de QUANTIDADE em QUANTIDADE.
PESQUISA_BASE = "https://pesquisa.apps.tcu.gov.br/rest/publico/base/processo/documentosResumidos"
PESQUISA_QUANTIDADE = 50
PESQUISA_MAX_PAGINAS = 60
PESQUISA_ORDENACAO = "DTAUTUACAOORDENACAO desc, NUMEROCOMZEROS desc, KEY asc"
# Uma consulta por unidade monitorada: o filtro casa o nome exato da UJ.
PESQUISA_UNIDADES = [
    "Ministério do Planejamento e Orçamento",
    "Secretaria de Orçamento Federal",
    "Secretaria Nacional de Planejamento",
    "Secretaria de Monitoramento e Avaliação",
    "Secretaria de Coordenação e Governança das Empresas Estatais",
]
PESQUISA_HEADERS = {"Accept": "application/json", "Referer": "https://pesquisa.apps.tcu.gov.br/"}
# ---------------------------------------------------------------------------

# Unidades jurisdicionadas monitoradas. O casamento é sobre o campo que o
# PRÓPRIO TCU declara — não é heurística sobre o texto do assunto.
UNIDADES: dict[str, dict[str, Any]] = {
    "MPO": {
        "nome": "Ministério do Planejamento e Orçamento",
        "padroes": [
            r"minist[eé]rio do planejamento e or[cç]amento",
            r"minist[eé]rio do planejamento, or[cç]amento e gest[aã]o",
            r"secretaria-?executiva do minist[eé]rio do planejamento",
            r"assessoria especial de controle interno do minist[eé]rio do planejamento",
            r"\bmpo\b",
        ],
    },
    "SOF": {"nome": "Secretaria de Orçamento Federal",
            "padroes": [r"secretaria de or[cç]amento federal", r"\bsof\b"]},
    "SEPLAN": {"nome": "Secretaria Nacional de Planejamento",
               "padroes": [r"secretaria nacional de planejamento", r"\bseplan\b"]},
    "SMA": {"nome": "Secretaria de Monitoramento e Avaliação",
            "padroes": [r"secretaria (nacional )?de monitoramento e avalia[cç][aã]o", r"\bsma\b"]},
    "SEST": {"nome": "Secretaria de Coordenação e Governança das Empresas Estatais",
             "padroes": [r"secretaria de coordena[cç][aã]o e governan[cç]a das empresas estatais", r"\bsest\b"]},
}

TIMEOUT = (10, 90)

# =========================================================================== #
# UTILIDADES
# =========================================================================== #


def normalizar(texto: Any) -> str:
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).lower().strip()


RX_UNIDADES = {s: [re.compile(p) for p in c["padroes"]] for s, c in UNIDADES.items()}


def orgaos_em(texto: str | None) -> list[str]:
    n = normalizar(texto)
    return [s for s, rxs in RX_UNIDADES.items() if any(rx.search(n) for rx in rxs)]


def so_digitos(numero: Any) -> str:
    return re.sub(r"\D", "", str(numero or ""))


def formatar_processo(numero: Any) -> str:
    d = so_digitos(numero)
    return f"{d[:3]}.{d[3:6]}/{d[6:10]}-{d[10]}" if len(d) == 11 else str(numero or "").strip()


def parse_data(valor: Any) -> datetime | None:
    if not valor:
        return None
    t = str(valor).strip()
    for f in (lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
              lambda s: datetime.strptime(s, "%d/%m/%Y"),
              lambda s: datetime.strptime(s, "%Y-%m-%d")):
        try:
            d = f(t)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def sessao_http() -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False)))
    s.headers.update({"Accept": "application/json",
                      "User-Agent": "painel-mpo/2.0 (monitoramento de dados abertos)"})
    return s


# =========================================================================== #
# FONTE 1 — BTCU
# =========================================================================== #

# Um processo aparece em várias roupagens ao longo do boletim. Exigir "número no
# começo da linha seguido de hífen" perde todas as relações, que é onde os
# acórdãos são publicados em lote.
RX_PROCESSO = re.compile(
    r"^\s*(?:\d{1,3}\s*[.)]\s*)?"
    r"(?:(?:Processo|Anexo|Apenso|Apensos?)\s*:?\s*)?"
    r"(?:TC[-\s]\s*)?"
    r"(\d{3}\.\d{3}/\d{4}-\d)\s*-?\s*", re.M)

RX_CAMPO = re.compile(
    r"(Natureza|Unidade [Jj]urisdicionada|[ÓO]rg[ãa]o/Entidade/Unidade|[ÓO]rg[ãa]o/Entidade|"
    r"Respons[áa]ve(?:l|is)|Interessad[oa]s?|Representa[çc][ãa]o legal|Recorrentes?|"
    r"Embargantes?|Representante|Solicitante|Exerc[íi]cio|Revisor|Advogad[oa]s?|"
    r"Interesse em sustenta[çc][ãa]o oral)\s*:")

RX_RELATOR = re.compile(
    r"^\s*(?:Ministr[oa]|MINISTR[OA])(?:[-\s]Substitut[oa]|[-\s]SUBSTITUT[OA])?\s+"
    r"([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-Za-zÁÂÃÉÊÍÓÔÕÚÇáâãéêíóôõúç\s.]{4,60})\s*$")
RX_COLEGIADO = re.compile(r"PAUTA (?:DO|DA) (PLEN[ÁA]RIO|PRIMEIRA C[ÂA]MARA|SEGUNDA C[ÂA]MARA)")
RX_SESSAO = re.compile(r"Sess[ãa]o\s+\w+\s+de\s+(\d{2}/\d{2}/\d{4})")
RX_SECAO = re.compile(r"^\s*(PAUTAS?|ATAS?|DESPACHOS DE AUTORIDADES|EDITAIS|"
                      r"ACORD[ÃA]OS|DELIBERA[ÇC][ÕO]ES)\s*$", re.I)
RX_ACORDAO = re.compile(
    r"AC[ÓO]RD[ÃA]O\s+N?[ºo°]?\s*([\d.]+)\s*/\s*(\d{4})\s*[-–]\s*TCU\s*[-–]\s*"
    r"(Plen[áa]rio|Primeira C[âa]mara|Segunda C[âa]mara|1[ªa] C[âa]mara|2[ªa] C[âa]mara)", re.I)
RX_RUIDO = re.compile(
    r"(Para verificar as assinaturas.*?\d{8}\.|BTCU Deliberações.*?\d{4}\s+\d+|"
    r"CODMATERIA=\d+|A presente pauta pode.*?RITCU\)\.|"
    r"As transmiss[õo]es das sess[õo]es.*?sessoes/\.)", re.S)

FASES = {
    "pauta": "Incluído em pauta",
    "ata": "Julgado",
    "despacho": "Despacho do relator",
    "edital": "Edital publicado",
    "indefinido": "Movimentação no boletim",
}


def _secao(titulo: str) -> str:
    t = normalizar(titulo)
    if t.startswith("pauta"):
        return "pauta"
    if t.startswith("ata") or "acorda" in t or "delibera" in t:
        return "ata"
    if "despacho" in t:
        return "despacho"
    if "edital" in t:
        return "edital"
    return "indefinido"


def _campo(bloco: str, rotulos: tuple[str, ...]) -> str | None:
    for rot in rotulos:
        m = re.search(rot + r"\s*:\s*(.+)", bloco, re.S)
        if not m:
            continue
        resto = m.group(1)
        fim = RX_CAMPO.search(resto)
        v = re.sub(r"\s+", " ", (resto[: fim.start()] if fim else resto)).strip().rstrip(".").strip()
        if v and normalizar(v) not in {"nao ha", "nao consta"}:
            return v
    return None


def ler_btcu(sessao: requests.Session, id_edicao: int) -> str | None:
    try:
        r = sessao.get(BTCU_URL.format(id=id_edicao), timeout=TIMEOUT)
        if r.status_code != 200 or not r.content[:5].startswith(b"%PDF"):
            return None
    except requests.exceptions.RequestException:
        return None
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit(
            "\nFALTA DEPENDÊNCIA: pypdf\n"
            "As edições do boletim são PDF. Instale com:  pip install pypdf\n"
            "e confirme que 'pypdf' está no requirements.txt do repositório.\n")
    try:
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)
    except Exception as exc:
        log.warning("Edição %s ilegível: %s", id_edicao, exc)
        return None


def extrair_movimentacoes(texto: str, id_edicao: int) -> list[dict]:
    """Uma movimentação por aparição de processo de interesse no boletim."""
    texto = re.sub(r"[ \t]+", " ", RX_RUIDO.sub(" ", texto))
    colegiado = data_sessao = relator = None
    secao, acordao = "indefinido", None
    saida: list[dict] = []
    buffer: list[str] = []
    numero: str | None = None

    def fechar() -> None:
        nonlocal buffer, numero
        if not numero:
            buffer = []
            return
        bloco = " ".join(buffer)
        unidade = _campo(bloco, (r"Unidade [Jj]urisdicionada",
                                 r"[ÓO]rg[ãa]o/Entidade/Unidade", r"[ÓO]rg[ãa]o/Entidade"))
        interessados = _campo(bloco, (r"Interessad[oa]s?",))
        na_unidade = orgaos_em(unidade)
        orgaos = na_unidade or orgaos_em(interessados)
        if orgaos:
            corte = RX_CAMPO.search(bloco)
            assunto = (bloco[: corte.start()] if corte else bloco).strip().rstrip(".")
            saida.append({
                "processo": numero,
                "orgaos": orgaos,
                "vinculo": ("unidade jurisdicionada" if na_unidade else "interessado"),
                "unidades": [u.strip() for u in (unidade or "").split(";") if u.strip()],
                "assunto": assunto or None,
                "natureza": _campo(bloco, (r"Natureza",)),
                "relator": relator,
                "colegiado": colegiado,
                "fase": FASES[secao],
                "acordao": acordao if secao == "ata" else None,
                "data": iso(parse_data(data_sessao)),
                "edicao": id_edicao,
            })
        buffer = []
        numero = None

    for linha in texto.split("\n"):
        if m := RX_SECAO.match(linha):
            fechar()
            secao = _secao(m.group(1))
            continue
        # Só é cabeçalho se ABRE a linha: um acórdão citado dentro do texto de um
        # monitoramento é referência, não a decisão deste processo.
        if m := RX_ACORDAO.match(linha.strip()):
            fechar()
            acordao = f"{m.group(1)}/{m.group(2)}"
            if secao == "indefinido":
                secao = "ata"
            continue
        if m := RX_COLEGIADO.search(linha):
            fechar()
            colegiado, secao = m.group(1).title().replace("Camara", "Câmara"), "pauta"
            continue
        if m := RX_SESSAO.search(linha):
            data_sessao = m.group(1)
            continue
        if m := RX_RELATOR.match(linha):
            fechar()
            relator = m.group(1).strip().title()
            continue
        if m := RX_PROCESSO.match(linha):
            fechar()
            numero = m.group(1)
            buffer = [linha[m.end():]]
            continue
        if numero is not None:
            buffer.append(linha)

    fechar()
    return saida


def varrer_btcu(sessao: requests.Session, ancora: int, maximo: int) -> tuple[list[dict], int]:
    teto = max(ancora, BTCU_ANCORA) + int((date.today() - BTCU_DATA_ANCORA).days * 1.1) + 40
    log.info("BTCU: varrendo de %d até no máximo %d", ancora, teto)

    movs: list[dict] = []
    misses, lidas, maior, atual = 0, 0, ancora, ancora
    while misses < BTCU_MISSES and lidas < maximo and atual <= teto:
        texto = ler_btcu(sessao, atual)
        if texto is None:
            misses += 1
        else:
            misses, lidas, maior = 0, lidas + 1, max(maior, atual)
            achados = extrair_movimentacoes(texto, atual)
            if achados:
                log.info("Edição %d: %d movimentações de interesse", atual, len(achados))
            movs.extend(achados)
        atual += 1
    log.info("BTCU: %d edições lidas, %d movimentações, âncora em %d", lidas, len(movs), maior)
    return movs, maior


# =========================================================================== #
# FONTE 2 — Pesquisa Integrada (a configurar)
# =========================================================================== #


# Movimentação da Pesquisa: "DD/MM/AAAA - HH:MM:SS - texto livre"
RX_MOV = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\s*-\s*[\d:]+\s*-\s*(.+)$")
# Acórdão dentro do título de uma peça: "Acórdão Nº 7615/2020-TCU-Primeira Câmara"
RX_PECA_ACORDAO = re.compile(r"AC[ÓO]RD[ÃA]O\s+N?[ºo°]?\s*([\d.]+)/(\d{4})\s*-\s*TCU\s*-\s*"
                             r"([\w\s]+?C[âa]mara|Plen[áa]rio)", re.I)


def _movimentacoes_pesquisa(brutas: list, pecas: list) -> list[dict]:
    """Converte as MOVIMENTACOES (texto) e localiza acórdãos entre as PECAS."""
    movs = []
    for linha in brutas or []:
        m = RX_MOV.match(str(linha))
        if m:
            movs.append({"data": iso(parse_data(m.group(1))), "descricao": m.group(2).strip(),
                         "fase": None, "acordao": None})
    for pe in pecas or []:
        titulo = pe.get("TITULO") or pe.get("ASSUNTO") or ""
        m = RX_PECA_ACORDAO.search(titulo)
        if m:
            movs.append({
                "data": iso(parse_data((pe.get("DTRELEVANCIA") or "")[:10])),
                "descricao": f"Acórdão {m.group(1)}/{m.group(2)} — {m.group(3).strip()}",
                "fase": "Julgado", "acordao": f"{m.group(1)}/{m.group(2)}",
            })
    movs.sort(key=lambda x: parse_data(x["data"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return movs


def _campos_pesquisa(it: dict) -> dict:
    """Mapeia um documento da Pesquisa Integrada. Nomes REAIS confirmados na resposta."""
    unidades = it.get("UNIDADESJURISDICIONADAS") or []
    if isinstance(unidades, str):
        unidades = [unidades]
    unidades = [u.strip() for u in unidades if u and u.strip()]
    texto_uj = " ; ".join(unidades)

    pecas = it.get("PECAS") or []
    movs = _movimentacoes_pesquisa(it.get("MOVIMENTACOES"), pecas)
    ultima = movs[0] if movs else None
    acordao = next((m["acordao"] for m in movs if m.get("acordao")), None)

    return {
        "processo": formatar_processo(it.get("NUMEROFORMATADO") or it.get("PROC")),
        "codigo": it.get("CODIGO"),
        "estado": it.get("ESTADO"),
        "relator": it.get("RELATOR"),
        "assunto": it.get("ASSUNTO") or it.get("TITULOCOMPLETO"),
        "natureza": it.get("TIPO"),
        "unidades": unidades,
        "orgaos": orgaos_em(texto_uj),
        "movimentacoes_pesquisa": movs,
        "ultima_pesquisa": ultima,
        "acordao": acordao,
        "url_push": it.get("URLSISTEMAPUSH"),
    }


def consultar_pesquisa(sessao: requests.Session) -> list[dict]:
    """
    Consulta o índice público de processos, uma unidade jurisdicionada por vez,
    paginando até esgotar. É a fonte do campo Estado (Aberto/Encerrado) e da
    lista completa — inclusive processos sem movimentação recente no boletim.
    """
    vistos: dict[str, dict] = {}
    houve_resposta = False

    for unidade in PESQUISA_UNIDADES:
        filtro = f'UNIDADESJURISDICIONADAS:("{unidade}")'
        inicio = 0
        for _ in range(PESQUISA_MAX_PAGINAS):
            params = {"termo": "*", "filtro": filtro, "ordenacao": PESQUISA_ORDENACAO,
                      "quantidade": PESQUISA_QUANTIDADE, "inicio": inicio}
            try:
                r = sessao.get(PESQUISA_BASE, params=params, headers=PESQUISA_HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                dados = r.json()
            except (requests.exceptions.RequestException, ValueError) as exc:
                log.warning("Pesquisa Integrada (%s, início %d): %s", unidade[:30], inicio, exc)
                break

            houve_resposta = True
            itens = dados if isinstance(dados, list) else (
                dados.get("documentos") or dados.get("items") or dados.get("resultado")
                or dados.get("content") or dados.get("hits") or [])
            if not itens:
                break

            for it in itens:
                campo = _campos_pesquisa(it)
                if campo["processo"]:
                    # Um processo pode casar em duas UJs; o primeiro que vier fica,
                    # mas acumulamos os órgãos.
                    ja = vistos.get(campo["processo"])
                    if ja:
                        ja["orgaos"] = sorted(set(ja["orgaos"]) | set(campo["orgaos"]))
                    else:
                        vistos[campo["processo"]] = campo

            if len(itens) < PESQUISA_QUANTIDADE:
                break
            inicio += PESQUISA_QUANTIDADE

        log.info("Pesquisa: %s → %d processos acumulados", unidade[:34], len(vistos))

    if not houve_resposta:
        log.error("Pesquisa Integrada não respondeu em nenhuma consulta.")
    return list(vistos.values())


# =========================================================================== #
# CONSOLIDAÇÃO
# =========================================================================== #

ORDEM_FASE = {"Incluído em pauta": 1, "Edital publicado": 2, "Movimentação no boletim": 3,
              "Despacho do relator": 4, "Julgado": 5}


def consolidar_pesquisa(processos_pesquisa: list[dict]) -> list[dict]:
    """A Pesquisa Integrada já traz tudo por processo: monta a saída direto dela."""
    saida = []
    for p in processos_pesquisa:
        movs = p.get("movimentacoes_pesquisa") or []
        # Dedup por (data, descrição) — a mesma movimentação pode repetir.
        vistas, limpas = set(), []
        for m in movs:
            chave = (m["data"], m["descricao"][:60])
            if chave not in vistas:
                vistas.add(chave)
                limpas.append(m)
        ultima = limpas[0] if limpas else None
        saida.append({
            "numero": p["processo"], "id": so_digitos(p["processo"]),
            "codigo": p.get("codigo"),
            "estado": p.get("estado"),
            "relator": p.get("relator"),
            "assunto": p.get("assunto"),
            "natureza": p.get("natureza"),
            "unidades": p.get("unidades") or [],
            "orgaos": sorted(p.get("orgaos") or []),
            "acordao": p.get("acordao"),
            "movimentacoes": limpas,
            "ultima_movimentacao": ultima,
            "fase_atual": ultima["descricao"][:80] if ultima else None,
            "atualizado_em": ultima["data"] if ultima else None,
            "url_push": p.get("url_push"),
        })
    saida.sort(key=lambda p: parse_data(p["atualizado_em"]) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return saida


def _consolidar_boletim(movs: list[dict], da_pesquisa: list[dict]) -> list[dict]:
    """Agrupa movimentações por processo e funde com o que veio da pesquisa."""
    proc: dict[str, dict] = {}

    for m in movs:
        p = proc.setdefault(m["processo"], {
            "numero": m["processo"], "id": so_digitos(m["processo"]),
            "movimentacoes": [], "orgaos": set(), "unidades": [],
            "relator": None, "colegiado": None, "assunto": None,
            "natureza": None, "estado": None, "vinculo": None, "abertura": None,
        })
        p["orgaos"].update(m["orgaos"])
        if m["unidades"] and not p["unidades"]:
            p["unidades"] = m["unidades"]
        for campo in ("relator", "colegiado", "natureza"):
            if m.get(campo) and not p[campo]:
                p[campo] = m[campo]
        # Fica o assunto mais completo: o boletim ora traz a ementa inteira,
        # ora só uma linha de relação.
        if m.get("assunto") and len(m["assunto"]) > len(p["assunto"] or ""):
            p["assunto"] = m["assunto"]
        if m["vinculo"] == "unidade jurisdicionada":
            p["vinculo"] = m["vinculo"]
        elif not p["vinculo"]:
            p["vinculo"] = m["vinculo"]
        p["movimentacoes"].append({
            "data": m["data"], "fase": m["fase"],
            "acordao": m.get("acordao"), "colegiado": m.get("colegiado"),
            "relator": m.get("relator"), "edicao": m.get("edicao"),
        })

    for p in da_pesquisa:
        alvo = proc.setdefault(p["processo"], {
            "numero": p["processo"], "id": so_digitos(p["processo"]),
            "movimentacoes": [], "orgaos": set(), "unidades": [],
            "relator": None, "colegiado": None, "assunto": None,
            "natureza": None, "estado": None,
            "vinculo": "unidade jurisdicionada", "abertura": None,
        })
        alvo["orgaos"].update(p.get("orgaos") or [])
        for campo in ("estado", "relator", "assunto", "natureza", "abertura"):
            if p.get(campo):
                alvo[campo] = p[campo]     # a pesquisa é autoritativa
        if p.get("unidades"):
            alvo["unidades"] = p["unidades"]

    saida = []
    for p in proc.values():
        movs_p = sorted(p["movimentacoes"],
                        key=lambda m: (parse_data(m["data"]) or datetime.min.replace(tzinfo=timezone.utc),
                                       ORDEM_FASE.get(m["fase"], 0)))
        # Deduplicar: a mesma fase na mesma data em edições diferentes é repetição.
        vistas, limpas = set(), []
        for m in movs_p:
            chave = (m["data"], m["fase"], m["acordao"])
            if chave not in vistas:
                vistas.add(chave)
                limpas.append(m)
        ultima = limpas[-1] if limpas else None
        saida.append({
            **p,
            "orgaos": sorted(p["orgaos"]),
            "movimentacoes": limpas,
            "ultima_movimentacao": ultima,
            "fase_atual": ultima["fase"] if ultima else None,
            "atualizado_em": ultima["data"] if ultima else None,
        })

    saida.sort(key=lambda p: parse_data(p["atualizado_em"]) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return saida


def montar(processos: list[dict], ancora: int, avisos: list[str]) -> dict:
    agora = datetime.now(timezone.utc)
    por_orgao = []
    for sigla, cfg in UNIDADES.items():
        do_orgao = [p for p in processos if sigla in p["orgaos"]]
        if do_orgao:
            por_orgao.append({"orgao": sigla, "nome": cfg["nome"], "total": len(do_orgao)})

    movs = [{**m, "processo": p["numero"], "assunto": p["assunto"],
             "orgaos": p["orgaos"], "estado": p["estado"]}
            for p in processos for m in p["movimentacoes"]]
    movs.sort(key=lambda m: parse_data(m["data"]) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)

    return {
        "versao": 1,
        "gerado_em": agora.isoformat(),
        "gerado_em_br": agora.astimezone().strftime("%d/%m/%Y às %H:%M"),
        "ancora_btcu": ancora,
        "tem_estado": any(p["estado"] for p in processos),
        "avisos": avisos,
        "totais": {
            "processos": len(processos),
            "movimentacoes": len(movs),
            "abertos": sum(1 for p in processos if normalizar(p["estado"]) == "aberto"),
        },
        "por_orgao": por_orgao,
        "processos": processos,
        "movimentacoes": movs[:200],
    }


def salvar(payload: dict, caminho: str) -> None:
    destino = os.path.abspath(caminho)
    pasta = os.path.dirname(destino) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, destino)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# =========================================================================== #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Processos do TCU com o MPO como unidade jurisdicionada.")
    ap.add_argument("--saida", default="site/dados.json")
    ap.add_argument("--com-boletim", action="store_true",
                    help="além da Pesquisa Integrada, varre o boletim para captar pauta futura")
    ap.add_argument("--desde-id", type=int, default=None,
                    help="com --com-boletim: id inicial do boletim (22110≈ago/2024)")
    ap.add_argument("--max-edicoes", type=int, default=60)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    http = sessao_http()
    avisos: list[str] = []

    # Fonte primária: Pesquisa Integrada. Traz a lista COMPLETA de processos do
    # MPO, com estado, relator, assunto, movimentações e acórdãos.
    da_pesquisa = consultar_pesquisa(http)
    if not da_pesquisa:
        avisos.append("A Pesquisa Integrada do TCU não respondeu nesta execução. "
                      "Tente novamente mais tarde; o site pode estar instável.")
        if os.path.exists(args.saida):
            log.warning("Nada coletado; %s anterior preservado.", args.saida)
            return 0
        log.error("Nada coletado e não há arquivo anterior.")
        return 1

    processos = consolidar_pesquisa(da_pesquisa)

    # Camada opcional: o boletim adiciona pauta futura (a Pesquisa não distingue
    # "vai ser julgado" de "foi julgado"). Só quando pedido, para não pesar.
    ancora = args.desde_id or BTCU_ANCORA
    if args.com_boletim:
        if not args.desde_id:
            try:
                with open(args.saida, encoding="utf-8") as f:
                    ancora = max(ancora, int(json.load(f).get("ancora_btcu", ancora)))
            except (OSError, ValueError, TypeError):
                pass
        movs, ancora = varrer_btcu(http, ancora, args.max_edicoes)
        emedados = {p["numero"] for p in processos}
        extras = [m for m in movs if m["processo"] not in emedados]
        if extras:
            processos += _consolidar_boletim(extras, [])
            log.info("Boletim: %d processos adicionais não vistos na Pesquisa", len(extras))

    salvar(montar(processos, ancora, avisos), args.saida)
    log.info("%s gravado: %d processos, %d movimentações.",
             args.saida, len(processos), sum(len(p["movimentacoes"]) for p in processos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
