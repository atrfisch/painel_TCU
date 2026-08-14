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
from datetime import date, datetime, timedelta, timezone
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
PESQUISA_MAX_PAGINAS = 120
PESQUISA_ORDENACAO = "DTAUTUACAOORDENACAO desc, NUMEROCOMZEROS desc, KEY asc"

# O filtro por unidade exige correspondência com a grafia cadastrada, que varia
# (o órgão já se chamou "Ministério da Economia" e "Ministério do Planejamento,
# Desenvolvimento e Gestão"; secretarias trocam de vínculo). Buscar por termo
# livre, além do filtro estruturado, recupera o que a grafia exata perderia.
PESQUISA_UNIDADES = [
    'UNIDADESJURISDICIONADAS:("Ministério do Planejamento e Orçamento")',
    'UNIDADESJURISDICIONADAS:("Secretaria de Orçamento Federal")',
    'UNIDADESJURISDICIONADAS:("SOF/MPO - Secretaria de Orçamento Federal")',
    'UNIDADESJURISDICIONADAS:("Secretaria Nacional de Planejamento")',
    'UNIDADESJURISDICIONADAS:("Secretaria de Monitoramento e Avaliação")',
    'UNIDADESJURISDICIONADAS:("Secretaria de Coordenação e Governança das Empresas Estatais")',
    'UNIDADESJURISDICIONADAS:("Assessoria Especial de Controle Interno do Ministério do Planejamento e Orçamento")',
    'UNIDADESJURISDICIONADAS:("Secretaria-Executiva do Ministério do Planejamento e Orçamento")',
    'UNIDADESJURISDICIONADAS:("Ministério do Planejamento, Desenvolvimento e Gestão")',
    'UNIDADESJURISDICIONADAS:("Ministério da Economia")',
]

# Filtros por INTERESSADO: capturam processos em que o órgão do MPO não é a
# unidade jurisdicionada, mas consta como parte interessada — o caso típico da
# AECI e da Secretaria-Executiva. Complementa o filtro por unidade.
PESQUISA_INTERESSADOS = [
    'INTERESSADOS:("Assessoria Especial de Controle Interno do Ministério do Planejamento e Orçamento")',
    'INTERESSADOS:("Secretaria-Executiva do Ministério do Planejamento e Orçamento")',
    'INTERESSADOS:("Ministério do Planejamento e Orçamento")',
    'INTERESSADOS:("Secretaria de Orçamento Federal")',
    'INTERESSADOS:("Secretaria de Monitoramento e Avaliação de Políticas Públicas e Assuntos Econômicos")',
    'INTERESSADOS:("Secretaria Nacional de Planejamento")',
]

# Termos livres — a rede mais ampla. Buscam o nome do órgão em QUALQUER campo
# indexado, inclusive "Órgãos/Entidades fiscalizados", onde o MPO e suas
# secretarias aparecem como co-fiscalizados em processos cujo órgão PRINCIPAL é
# outro (tipicamente o Ministério da Fazenda). É o que captura fiscalizações
# conjuntas que os filtros estruturados de unidade não trazem.
PESQUISA_TERMOS = [
    '"Ministério do Planejamento e Orçamento"',
    '"Secretaria de Orçamento Federal"',
    '"Secretaria Nacional de Planejamento"',
    '"Secretaria de Monitoramento e Avaliação de Políticas Públicas e Assuntos Econômicos"',
    '"Assessoria Especial de Controle Interno do Ministério do Planejamento e Orçamento"',
    '"Secretaria-Executiva do Ministério do Planejamento e Orçamento"',
]

# Endpoint de detalhe por número: traz MOVIMENTACOES e PECAS que a listagem
# resumida não inclui. Descoberto pelo padrão /doc/processo/{PROC sem zeros}.
PESQUISA_DETALHE = "https://pesquisa.apps.tcu.gov.br/rest/publico/base/processo/documento"

# Processos que devem entrar SEMPRE, buscados um a um pelo número — independem
# de o filtro de unidade os capturar. É a rede de segurança para os que somem.
#
# A lista fica num arquivo TEXTO separado (processos-acompanhados.txt), que pode
# ser editado direto pelo GitHub sem tocar no código. A lista abaixo é só o
# fallback, usado se o arquivo não existir.
_GARANTIDOS_FALLBACK = [
    "022.756/2025-6", "005.405/2026-2", "022.852/2025-5", "017.106/2025-7",
    "025.632/2024-8", "005.104/2023-8", "007.158/2026-2", "011.685/2026-3",
    "011.526/2022-0", "008.723/2023-0",
    "011.358/2026-2", "024.312/2024-0", "024.381/2025-0",
]

RX_NUM_PROCESSO = re.compile(r"\d{3}\.\d{3}/\d{4}-\d")


def carregar_garantidos(caminho: str = "processos-acompanhados.txt") -> list[str]:
    """Lê a lista de processos acompanhados do arquivo texto. Ignora comentários
    (#) e linhas em branco. Cai no fallback embutido se o arquivo não existir."""
    try:
        with open(caminho, encoding="utf-8") as f:
            numeros = []
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#"):
                    continue
                m = RX_NUM_PROCESSO.search(linha)
                if m:
                    numeros.append(m.group(0))
        if numeros:
            log.info("Acompanhados: %d processos lidos de %s", len(numeros), caminho)
            return numeros
    except OSError:
        pass
    log.info("Acompanhados: usando lista embutida (%d processos)", len(_GARANTIDOS_FALLBACK))
    return _GARANTIDOS_FALLBACK


PROCESSOS_GARANTIDOS = _GARANTIDOS_FALLBACK  # substituído em tempo de execução

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
            r"\bmpo\b",
        ],
    },
    "AECI": {
        "nome": "Assessoria Especial de Controle Interno do MPO",
        "padroes": [
            r"assessoria especial de controle interno do minist[eé]rio do planejamento",
            r"\baeci\b",
        ],
    },
    "SE": {
        "nome": "Secretaria-Executiva do MPO",
        "padroes": [
            r"secretaria-?executiva do minist[eé]rio do planejamento",
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
    t = str(texto)
    # A Pesquisa destaca os termos buscados com <em>...</em> dentro dos próprios
    # valores (nome de unidade, assunto). Sem remover, "SOF/MPO - <em>Secretaria</em>"
    # entra sujo e pode furar o casamento de órgão.
    t = re.sub(r"</?em>", "", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).lower().strip()


def limpar_html(texto: Any) -> str:
    """Remove o realce <em> que a API insere nos valores."""
    return re.sub(r"</?em>", "", str(texto or "")).strip()


RX_UNIDADES = {s: [re.compile(p) for p in c["padroes"]] for s, c in UNIDADES.items()}


def orgaos_em(texto: str | None) -> list[str]:
    """
    Casa órgãos por UNIDADE. A lista de UJs vem separada por ';' — avalio cada
    uma isolada para não misturar. AECI e SE contêm "Ministério do Planejamento
    e Orçamento" no nome, então numa mesma unidade elas têm precedência sobre o
    MPO: uma UJ é a Assessoria de Controle Interno OU o Ministério, não os dois.
    """
    achados: set[str] = set()
    for parte in re.split(r"[;\n]", texto or ""):
        n = normalizar(parte)
        if not n:
            continue
        casaram = [s for s, rxs in RX_UNIDADES.items() if any(rx.search(n) for rx in rxs)]
        if ("AECI" in casaram or "SE" in casaram) and "MPO" in casaram:
            casaram.remove("MPO")  # a UJ específica prevalece sobre o guarda-chuva
        achados.update(casaram)
    return sorted(achados)


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
    unidades = [limpar_html(u) for u in unidades if u and str(u).strip()]

    # Órgãos/Entidades FISCALIZADOS: em fiscalizações conjuntas, o processo é
    # atribuído ao órgão PRINCIPAL (muitas vezes o Ministério da Fazenda), e o
    # MPO e suas secretarias entram como co-fiscalizados. Esse campo — que a
    # página do processo mostra como "Órgãos/Entidades fiscalizados" — nem sempre
    # aparece na listagem; tentamos vários nomes prováveis. Ser fiscalizado é
    # vínculo de UNIDADE (não mero interesse): o órgão é objeto da fiscalização.
    fiscalizados = (it.get("ORGAOSFISCALIZADOS") or it.get("ENTIDADESFISCALIZADAS")
                    or it.get("FISCALIZADOS") or it.get("ORGAOS") or [])
    if isinstance(fiscalizados, str):
        fiscalizados = [fiscalizados]
    fiscalizados = [limpar_html(f) for f in fiscalizados if f and str(f).strip()]
    # Unidades = jurisdicionadas + fiscalizadas (ambas geram vínculo de unidade).
    unidades_todas = list(dict.fromkeys(unidades + fiscalizados))
    texto_uj = " ; ".join(unidades_todas)

    # Além da unidade jurisdicionada, um processo pode ter órgãos do MPO como
    # INTERESSADOS — ex.: a Assessoria de Controle Interno ou a Secretaria-
    # Executiva do MPO. A busca em massa costuma vir SEM esse campo (vem vazio);
    # nesse caso, o vínculo desses órgãos aparece no TEXTO das movimentações
    # ("... em nome de Secretaria-Executiva do Ministério do Planejamento..."),
    # de onde também os extraímos.
    interessados = (it.get("INTERESSADOS") or it.get("INTERESSADO")
                    or it.get("PARTES") or [])
    if isinstance(interessados, str):
        interessados = [interessados]
    interessados = [limpar_html(i) for i in interessados if i and str(i).strip()]

    responsaveis = it.get("RESPONSAVEIS") or []
    if isinstance(responsaveis, str):
        responsaveis = [responsaveis]
    responsaveis = [limpar_html(r) for r in responsaveis if r and str(r).strip()]

    movs_brutas = it.get("MOVIMENTACOES") or []
    texto_movs = " ; ".join(str(m) for m in movs_brutas)

    texto_int = " ; ".join(interessados + responsaveis)

    orgaos_uj = set(orgaos_em(texto_uj))
    orgaos_int = set(orgaos_em(texto_int))
    # AECI e SE também valem quando citadas nas movimentações (padrão comum:
    # comunicações "em nome de" essas unidades). Só essas duas, para não capturar
    # menções incidentais de outros órgãos no corpo do andamento.
    orgaos_mov = {s for s in ("AECI", "SE") if s in orgaos_em(texto_movs)}
    orgaos_int |= orgaos_mov

    orgaos = sorted(orgaos_uj | orgaos_int)
    vinculo = "unidade" if orgaos_uj else ("interessado" if orgaos_int else None)

    pecas = it.get("PECAS") or []
    movs = _movimentacoes_pesquisa(it.get("MOVIMENTACOES"), pecas)
    ultima = movs[0] if movs else None
    acordao = next((m["acordao"] for m in movs if m.get("acordao")), None)

    return {
        "processo": formatar_processo(it.get("NUMEROFORMATADO") or it.get("PROC")),
        "codigo": it.get("CODIGO"),
        "estado": it.get("ESTADO"),
        "relator": limpar_html(it.get("RELATOR")) or None,
        "assunto": limpar_html(it.get("ASSUNTO") or it.get("TITULOCOMPLETO")) or None,
        "natureza": limpar_html(it.get("TIPO")) or None,
        "unidades": unidades_todas,
        "interessados": interessados,
        "orgaos": orgaos,
        "orgaos_unidade": sorted(orgaos_uj),
        "orgaos_interessado": sorted(orgaos_int - orgaos_uj),
        "vinculo": vinculo,
        "movimentacoes_pesquisa": movs,
        "ultima_pesquisa": ultima,
        "acordao": acordao,
        "url_push": it.get("URLSISTEMAPUSH"),
    }


def _uma_consulta(sessao: requests.Session, termo: str, filtro: str, rotulo: str,
                  vistos: dict[str, dict]) -> bool:
    """Executa uma consulta paginada e acumula em `vistos`. Devolve se respondeu."""
    inicio = 0
    respondeu = False
    for _ in range(PESQUISA_MAX_PAGINAS):
        params = {"termo": termo, "ordenacao": PESQUISA_ORDENACAO,
                  "quantidade": PESQUISA_QUANTIDADE, "inicio": inicio}
        if filtro:
            params["filtro"] = filtro
        try:
            r = sessao.get(PESQUISA_BASE, params=params, headers=PESQUISA_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            dados = r.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            log.warning("Pesquisa (%s, início %d): %s", rotulo[:34], inicio, exc)
            break
        respondeu = True
        itens = dados if isinstance(dados, list) else (
            dados.get("documentos") or dados.get("items") or dados.get("resultado")
            or dados.get("content") or dados.get("hits") or [])
        if not itens:
            break
        for it in itens:
            campo = _campos_pesquisa(it)
            if not campo["processo"]:
                continue
            # Termo livre casa qualquer texto: confirmamos que ALGUM órgão do MPO
            # está mesmo entre as unidades, para não trazer processo alheio que
            # só menciona "planejamento" no assunto.
            if not campo["orgaos"]:
                continue
            ja = vistos.get(campo["processo"])
            if ja:
                ja["orgaos"] = sorted(set(ja["orgaos"]) | set(campo["orgaos"]))
            else:
                vistos[campo["processo"]] = campo
        if len(itens) < PESQUISA_QUANTIDADE:
            break
        inicio += PESQUISA_QUANTIDADE
    return respondeu


def buscar_por_numero(sessao: requests.Session, numero: str) -> dict | None:
    """Busca um processo específico pelo número. Rede de segurança para os que
    o filtro de unidade não captura."""
    proc_id = so_digitos(numero)
    for termo in (numero, proc_id):
        try:
            params = {"termo": termo, "quantidade": 5, "inicio": 0}
            r = sessao.get(PESQUISA_BASE, params=params, headers=PESQUISA_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            itens = r.json().get("documentos") or []
        except (requests.exceptions.RequestException, ValueError):
            continue
        for it in itens:
            campo = _campos_pesquisa(it)
            if so_digitos(campo["processo"]) == proc_id:
                return campo
    return None


def enriquecer_detalhe(sessao: requests.Session, campo: dict) -> bool:
    """
    Busca o detalhe do processo para obter INTERESSADOS e RESPONSAVEIS, que a
    busca em massa não traz (vêm vazios). É a via que finalmente captura AECI, SE
    e os órgãos do MPO que aparecem só como interessados — o vínculo que o
    Conecta-TCU mostra atrás de login, mas que também existe no detalhe público.

    Devolve True se conseguiu reclassificar os órgãos do processo.
    """
    proc_id = so_digitos(campo["processo"])
    if not proc_id:
        return False
    # Tenta o código interno (mais preciso) e, como alternativa, o número.
    chaves = [campo.get("codigo"), proc_id]
    for key in [k for k in chaves if k]:
        try:
            r = sessao.get(PESQUISA_DETALHE, params={"key": key},
                           headers=PESQUISA_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            doc = r.json()
        except (requests.exceptions.RequestException, ValueError):
            continue
        # A resposta pode vir embrulhada de várias formas.
        if isinstance(doc, dict):
            doc = doc.get("documento") or doc.get("documentos") or doc
        if isinstance(doc, list):
            doc = doc[0] if doc else None
        if not isinstance(doc, dict):
            continue

        detalhado = _campos_pesquisa(doc)
        # Reclassifica os órgãos: o detalhe pode revelar AECI/SE/etc como
        # interessados que a listagem resumida não mostrava.
        antes = set(campo.get("orgaos") or [])
        depois = set(detalhado.get("orgaos") or []) | antes
        if depois:
            campo["orgaos"] = sorted(depois)
            campo["orgaos_unidade"] = sorted(set(campo.get("orgaos_unidade") or [])
                                             | set(detalhado.get("orgaos_unidade") or []))
            campo["orgaos_interessado"] = sorted(set(campo.get("orgaos_interessado") or [])
                                                 | set(detalhado.get("orgaos_interessado") or []))
            if detalhado.get("interessados") and not campo.get("interessados"):
                campo["interessados"] = detalhado["interessados"]
            # Vínculo: unidade prevalece; senão interessado.
            if campo.get("orgaos_unidade"):
                campo["vinculo"] = "unidade"
            elif campo.get("orgaos_interessado"):
                campo["vinculo"] = campo.get("vinculo") or "interessado"
        # Preenche o que faltava.
        for k in ("movimentacoes_pesquisa", "ultima_pesquisa", "acordao",
                  "relator", "assunto", "natureza", "estado"):
            if detalhado.get(k) and not campo.get(k):
                campo[k] = detalhado[k]
        return bool(depois - antes)
    return False


def consultar_pesquisa(sessao: requests.Session, garantidos: list[str] | None = None) -> list[dict]:
    """
    Descobre os processos do MPO por três caminhos complementares:
      1. filtro estruturado por unidade (várias grafias, inclusive as antigas);
      2. busca por termo livre (pega grafias que o filtro exato perde);
      3. busca direta pelos números garantidos (rede de segurança).
    Depois enriquece cada um com as movimentações do endpoint de detalhe.
    """
    vistos: dict[str, dict] = {}
    houve_resposta = False
    garantidos = garantidos if garantidos is not None else PROCESSOS_GARANTIDOS

    for filtro in PESQUISA_UNIDADES:
        if _uma_consulta(sessao, "*", filtro, filtro.split('("')[-1], vistos):
            houve_resposta = True
    log.info("Após filtro por unidade: %d processos", len(vistos))

    for filtro in PESQUISA_INTERESSADOS:
        if _uma_consulta(sessao, "*", filtro, "int " + filtro.split('("')[-1], vistos):
            houve_resposta = True
    log.info("Após filtro por interessado: %d processos", len(vistos))

    for termo in PESQUISA_TERMOS:
        if _uma_consulta(sessao, termo, "", "termo " + termo, vistos):
            houve_resposta = True
    log.info("Após busca por termo livre: %d processos", len(vistos))

    for numero in garantidos:
        if numero in vistos:
            vistos[numero]["garantido"] = True
            continue
        campo = buscar_por_numero(sessao, numero)
        if campo:
            campo["garantido"] = True
            # Garantido que não casou nenhum órgão do MPO nos campos: ainda assim
            # entra (você o marcou como relevante), com vínculo próprio para o
            # painel poder exibi-lo com uma tag "acompanhado" em vez de nenhuma.
            if not campo.get("orgaos"):
                campo["vinculo"] = campo.get("vinculo") or "acompanhado"
            vistos[numero] = campo
            log.info("Garantido recuperado: %s (órgãos: %s)",
                     numero, campo.get("orgaos") or "nenhum reconhecido")
        else:
            log.warning("Garantido NÃO encontrado na base: %s", numero)

    # Enriquecimento por detalhe: a busca em massa não traz INTERESSADOS nem
    # RESPONSAVEIS (vêm vazios), então AECI, SE e órgãos do MPO que só constam
    # como interessados ficam invisíveis. Buscar o detalhe de cada processo
    # revela esse vínculo — é o mesmo dado que o Conecta-TCU mostra atrás de
    # login. Custa uma requisição por processo; como são dezenas (não a base
    # inteira), o custo é aceitável numa execução que roda de madrugada.
    todos = list(vistos.values())
    log.info("Enriquecendo %d processos com o detalhe (interessados, movimentações)", len(todos))
    revelados = 0
    falhas_seguidas = 0
    houve_sucesso = False
    for i, campo in enumerate(todos, 1):
        try:
            mudou = enriquecer_detalhe(sessao, campo)
            houve_sucesso = True
            falhas_seguidas = 0
            if mudou:
                revelados += 1
        except Exception:
            falhas_seguidas += 1
        # Se o endpoint de detalhe não responde de cara, aborta para não travar a
        # coleta inteira em dezenas de timeouts. A lista segue com o que já tem.
        if falhas_seguidas >= 8 and not houve_sucesso:
            log.warning("Endpoint de detalhe não respondeu nas primeiras %d tentativas; "
                        "pulando o enriquecimento. Interessados podem faltar.", falhas_seguidas)
            break
        if i % 25 == 0:
            log.info("  ... %d/%d enriquecidos", i, len(todos))
    log.info("Detalhe: %d processos ganharam órgão novo (AECI/SE/etc via interessado)", revelados)

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
            "interessados": p.get("interessados") or [],
            "orgaos": sorted(p.get("orgaos") or []),
            "orgaos_unidade": p.get("orgaos_unidade") or [],
            "orgaos_interessado": p.get("orgaos_interessado") or [],
            "vinculo": p.get("vinculo"),
            "acordao": p.get("acordao"),
            "movimentacoes": limpas,
            "ultima_movimentacao": ultima,
            "fase_atual": ultima["descricao"][:80] if ultima else None,
            "atualizado_em": ultima["data"] if ultima else None,
            "url_push": p.get("url_push"),
            "garantido": p.get("garantido", False),
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


def _dia(iso_str: str | None) -> str | None:
    d = parse_data(iso_str)
    return d.date().isoformat() if d else None


def montar(processos: list[dict], ancora: int, avisos: list[str],
           anterior: dict | None = None) -> dict:
    agora = datetime.now(timezone.utc)
    hoje = agora.date()

    # FOCO EM ABERTOS: encerrados saem de todo o painel. Guardamos a contagem
    # só para registrar quantos foram descartados.
    total_bruto = len(processos)
    encerrados = sum(1 for p in processos if normalizar(p.get("estado")) != "aberto")
    processos = [p for p in processos if normalizar(p.get("estado")) == "aberto"]

    # --- Novidades desde a coleta anterior --------------------------------
    # "Novo" = processo que não existia no dados.json anterior.
    # "Andamento" = movimentação cuja data é de ontem ou hoje (não estava, ou
    # o processo ganhou movimento recente). Comparar com a coleta anterior é o
    # que permite dizer "entrou no radar ontem" com honestidade.
    numeros_antes: set[str] = set()
    movs_antes: dict[str, set] = {}
    if anterior and isinstance(anterior.get("processos"), list):
        for p in anterior["processos"]:
            numeros_antes.add(p.get("numero"))
            movs_antes[p.get("numero")] = {
                (m.get("data"), (m.get("descricao") or "")[:60])
                for m in (p.get("movimentacoes") or [])
            }

    # Andamentos: o que é NOVO em relação à coleta anterior. Duas fontes de
    # verdade, combinadas para robustez:
    #  - comparação com a coleta anterior (o que não estava lá é novo), que
    #    resiste a falhas: se a coleta pulou um dia, o movimento de dois dias
    #    atrás ainda aparece por não ter sido visto antes;
    #  - janela de data (últimos 3 dias) como teto, para a primeira coleta com
    #    baseline não despejar meses de histórico de uma vez.
    limite_and = (hoje - timedelta(days=3)).isoformat()
    novos_processos = []
    andamentos_novos = []
    for p in processos:
        eh_novo = numeros_antes and p["numero"] not in numeros_antes
        if eh_novo:
            novos_processos.append({
                "processo": p["numero"], "orgaos": p["orgaos"],
                "assunto": p["assunto"], "natureza": p.get("natureza"),
                "relator": p.get("relator"), "estado": p.get("estado"),
            })
        conhecidas = movs_antes.get(p["numero"], set())
        for m in (p.get("movimentacoes") or []):
            dia = _dia(m.get("data"))
            if not dia or dia < limite_and:
                continue
            chave = (m.get("data"), (m.get("descricao") or "")[:60])
            # O critério principal é "não estava na coleta anterior". A janela de
            # data só evita o despejo inicial. Sem baseline, nada é novo ainda.
            if not numeros_antes:
                continue
            if chave in conhecidas:
                continue
            andamentos_novos.append({
                "processo": p["numero"], "orgaos": p["orgaos"],
                "assunto": p["assunto"], "data": m.get("data"),
                "descricao": m.get("descricao"), "acordao": m.get("acordao"),
                "novo_no_radar": eh_novo,
            })
    andamentos_novos.sort(key=lambda a: a["data"] or "", reverse=True)

    por_orgao = []
    for sigla, cfg in UNIDADES.items():
        do_orgao = [p for p in processos if sigla in p["orgaos"]]
        if do_orgao:
            como_unidade = sum(1 for p in do_orgao if sigla in (p.get("orgaos_unidade") or []))
            por_orgao.append({"orgao": sigla, "nome": cfg["nome"], "total": len(do_orgao),
                              "como_unidade": como_unidade,
                              "como_interessado": len(do_orgao) - como_unidade})

    # Quantos processos entram por unidade jurisdicionada vs só como interessado.
    so_interesse = sum(1 for p in processos if p.get("vinculo") == "interessado")

    # Distribuição por tipo (todos já são abertos).
    tipos: dict[str, int] = {}
    for p in processos:
        t = (p.get("natureza") or "Não classificado").strip()
        tipos[t] = tipos.get(t, 0) + 1
    por_tipo = sorted(({"tipo": k, "total": v, "abertos": v} for k, v in tipos.items()),
                      key=lambda x: -x["total"])

    # Distribuição por relator — terceiro gráfico do topo.
    relatores: dict[str, int] = {}
    for p in processos:
        r = (p.get("relator") or "Não distribuído").strip()
        relatores[r] = relatores.get(r, 0) + 1
    por_relator = sorted(({"relator": k, "total": v} for k, v in relatores.items()),
                         key=lambda x: -x["total"])

    movs = [{**m, "processo": p["numero"], "assunto": p["assunto"],
             "natureza": p.get("natureza"), "orgaos": p["orgaos"], "estado": p["estado"]}
            for p in processos for m in p["movimentacoes"]]
    movs.sort(key=lambda m: parse_data(m["data"]) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)

    # Ranking do último mês: processos com mais movimentações nos últimos 30
    # dias. Vira gráfico de barras clicável no painel.
    limite_30 = (hoje - timedelta(days=30)).isoformat()
    contagem_mes: dict[str, dict] = {}
    for p in processos:
        recentes_p = [m for m in p["movimentacoes"] if (_dia(m["data"]) or "") >= limite_30]
        if recentes_p:
            contagem_mes[p["numero"]] = {
                "processo": p["numero"], "orgaos": p["orgaos"],
                "assunto": p["assunto"], "relator": p.get("relator"),
                "movimentacoes_mes": len(recentes_p),
            }
    ranking_mes = sorted(contagem_mes.values(), key=lambda x: -x["movimentacoes_mes"])[:8]

    # Movimentações da última semana — SÓ inclusão em pauta ou acórdão publicado.
    # O feed deixa de listar despachos internos e passa a marcar apenas os
    # eventos de peso: entrou em pauta (vai a julgamento) ou saiu acórdão.
    def evento_relevante(m: dict) -> str | None:
        if m.get("acordao"):
            return "acordao"
        desc = normalizar(m.get("descricao") or m.get("fase"))
        # "incluído em pauta", "incluida em pauta", "inclusão em pauta"
        if "pauta" in desc and ("inclu" in desc or "pautad" in desc):
            return "pauta"
        return None

    limite_feed = (hoje - timedelta(days=30)).isoformat()
    recentes = []
    for m in movs:
        if (_dia(m["data"]) or "") < limite_feed:
            continue
        tipo_ev = evento_relevante(m)
        if tipo_ev:
            recentes.append({**m, "evento": tipo_ev})

    return {
        "versao": 2,
        "gerado_em": agora.isoformat(),
        "gerado_em_br": agora.astimezone().strftime("%d/%m/%Y às %H:%M"),
        "ancora_btcu": ancora,
        "tem_estado": any(p["estado"] for p in processos),
        "avisos": avisos,
        "totais": {
            "processos": len(processos),
            "movimentacoes": len(movs),
            "abertos": len(processos),
            "encerrados_ocultos": encerrados,
            "so_interesse": so_interesse,
        },
        "garantidos": [p["numero"] for p in processos if p.get("garantido")],
        "novos_processos": novos_processos,
        "andamentos_novos": andamentos_novos[:40],
        "tem_baseline": bool(numeros_antes),
        "por_orgao": por_orgao,
        "por_tipo": por_tipo,
        "por_relator": por_relator,
        "ranking_mes": ranking_mes,
        "processos": processos,
        "movimentacoes_recentes": recentes[:60],
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
    garantidos = carregar_garantidos()
    da_pesquisa = consultar_pesquisa(http, garantidos)
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

    # Carrega a coleta anterior ANTES de sobrescrever, para detectar o que é novo
    # desde ontem. Sem baseline (primeira execução), nada é marcado como novo.
    anterior = None
    try:
        with open(args.saida, encoding="utf-8") as f:
            anterior = json.load(f)
    except (OSError, ValueError, TypeError):
        anterior = None

    salvar(montar(processos, ancora, avisos, anterior), args.saida)
    log.info("%s gravado: %d processos, %d movimentações.",
             args.saida, len(processos), sum(len(p["movimentacoes"]) for p in processos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
