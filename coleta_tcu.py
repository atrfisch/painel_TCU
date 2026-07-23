#!/usr/bin/env python3
"""
coleta_tcu.py — coleta dados públicos do TCU relacionados ao MPO e suas secretarias.

Fontes efetivamente disponíveis (verificadas em 2026-07):
  1. Solicitações do Congresso Nacional (SCN)  -> contas.tcu.gov.br/ords/api/publica/scn
  2. Acórdãos                                  -> dados-abertos.apps.tcu.gov.br/api/acordao
  3. Pautas de sessão                          -> dados-abertos.apps.tcu.gov.br/api/pautassessao

Não existe endpoint público de "processos por unidade jurisdicionada". A atribuição
ao órgão é feita por casamento textual (assunto / sumário / título) contra padrões
declarados em ORGAOS. Isso é heurística: o campo `confianca` registra em que campo
houve o casamento, para permitir revisão manual.

Uso:
    python coleta_tcu.py --saida dados.json
    python coleta_tcu.py --sem-acordaos          # pula a fonte instável
    python coleta_tcu.py --max-acordaos 20000 -v
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("coleta_tcu")

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #

SCN_URL = "https://contas.tcu.gov.br/ords/api/publica/scn/pedidos_congresso"
ACORDAOS_URL = "https://dados-abertos.apps.tcu.gov.br/api/acordao/recupera-acordaos"
PAUTAS_URL = "https://dados-abertos.apps.tcu.gov.br/api/pautassessao"

TIMEOUT = (10, 60)  # (connect, read)
LOTE_ACORDAOS = 500
MAX_PAGINAS_SCN = 100

# Padrões de identificação. São aplicados sobre texto normalizado (sem acento,
# minúsculo). \b evita que "SMA" case dentro de "smartphone" ou "Itaipava".
ORGAOS: dict[str, dict[str, Any]] = {
    "MPO": {
        "nome": "Ministério do Planejamento e Orçamento",
        "padroes": [
            r"\bmpo\b",
            r"minist[eé]rio do planejamento e or[cç]amento",
            r"minist[eé]rio do planejamento\b",
        ],
    },
    "SOF": {
        "nome": "Secretaria de Orçamento Federal",
        "padroes": [r"\bsof\b", r"secretaria de or[cç]amento federal"],
    },
    "SEPLAN": {
        "nome": "Secretaria Nacional de Planejamento",
        "padroes": [r"\bseplan\b", r"secretaria nacional de planejamento"],
    },
    "SMA": {
        "nome": "Secretaria de Monitoramento e Avaliação",
        "padroes": [
            r"\bsma\b",
            r"secretaria de monitoramento e avalia[cç][aã]o",
            r"secretaria nacional de monitoramento e avalia[cç][aã]o",
        ],
    },
}

# Temas de interesse para o painel: permite cruzar "assunto x matéria" sem
# releitura manual. Um item pode receber vários temas.
#
# Os padrões são RADICAIS, não palavras inteiras. O português flexiona demais
# para casar formas exatas: "gasto tributário" não encontra "gastos tributários",
# e "avaliação de política" não encontra "avaliar políticas públicas". Escreva
# até o ponto em que a palavra ainda é inequívoca e pare ali.
TEMAS: dict[str, list[str]] = {
    "Orçamento e execução": [
        r"or[cç]ament", r"\bldo\b", r"\bloa\b", r"\bppa\b",
        r"contingenciament", r"limita[cç][aã]o de empenho", r"empenh",
        r"dota[cç][aã]o", r"cr[eé]dito (suplementar|extraordin[aá]rio|especial)",
    ],
    "Emendas parlamentares": [
        r"emendas? (parlamentar|individual|de relator|de bancada|de comiss[aã]o)",
        r"\brp\s?9\b", r"\brp\s?8\b",
    ],
    "Regras fiscais": [
        r"arcabou[cç]o fiscal", r"regras? fiscal", r"regras? fiscais", r"metas? fiscal",
        r"responsabilidade fiscal", r"\blrf\b", r"resultado prim[aá]rio", r"teto de gast",
    ],
    "Transferências e convênios": [
        r"transfer[eê]ncia", r"conv[eê]nio", r"transferegov", r"repasse",
        r"termos? de fomento", r"instrumentos? de repasse",
    ],
    "Planejamento e avaliação de políticas": [
        r"avalia(r|[cç][aã]o d[eo]|ndo) pol[ií]tica", r"pol[ií]ticas? p[uú]blicas?",
        r"monitorament", r"planejamento governamental", r"plano plurianual",
        r"gastos? tribut", r"ren[uú]ncias? (de )?receita", r"ren[uú]ncia fiscal",
    ],
    "Governança e TI": [
        r"governan[cç]a", r"tecnologia da informa[cç][aã]o",
        r"contrata[cç][aã]o de ti\b", r"transforma[cç][aã]o digital",
    ],
}

# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def normalizar(texto: str | None) -> str:
    """Minúsculas, sem acento, espaços colapsados — base para o casamento textual."""
    if not texto:
        return ""
    txt = unicodedata.normalize("NFKD", str(texto))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", txt).lower().strip()


def _compilar(padroes: dict[str, list[str]]) -> dict[str, list[re.Pattern[str]]]:
    return {k: [re.compile(p) for p in v] for k, v in padroes.items()}


RX_ORGAOS = _compilar({s: c["padroes"] for s, c in ORGAOS.items()})
RX_TEMAS = _compilar(TEMAS)


def identificar(texto_norm: str, regras: dict[str, list[re.Pattern[str]]]) -> list[str]:
    return [chave for chave, rxs in regras.items() if any(rx.search(texto_norm) for rx in rxs)]


def classificar(texto: str) -> dict[str, Any] | None:
    """
    Seleção em duas camadas.

      nominal  — o texto cita o órgão ("MPO", "Secretaria de Orçamento Federal").
                 Alta confiança, volume baixo.
      temática — o texto trata de matéria da competência do MPO (LDO, LOA, PPA,
                 regra fiscal, emendas, transferências). Confiança média, volume alto.

    Filtrar só por nome do órgão devolve quase nada: as solicitações do Congresso
    descrevem o objeto ("supressão de cláusula na LDO 2026"), não a pasta
    responsável. Devolve None quando o item não interessa a nenhuma das camadas.
    """
    norm = normalizar(texto)
    orgaos = identificar(norm, RX_ORGAOS)
    temas = identificar(norm, RX_TEMAS)
    if not orgaos and not temas:
        return None
    return {
        "orgaos": orgaos,
        "temas": temas,
        "confianca": "nominal" if orgaos else "tematica",
    }


def limpar_processo(numero: str | None) -> str:
    """'011.503/2026-2' -> '01150320262' (formato aceito pelo endpoint de detalhe)."""
    return re.sub(r"\D", "", numero or "")


def formatar_processo(numero: str | None) -> str:
    """Devolve o número no formato canônico NNN.NNN/AAAA-D quando possível."""
    d = limpar_processo(numero)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}/{d[6:10]}-{d[10]}"
    return (numero or "").strip()


def parse_data(valor: Any) -> datetime | None:
    """Aceita ISO-8601 (com Z) e DD/MM/AAAA. Devolve datetime tz-aware em UTC."""
    if not valor:
        return None
    texto = str(valor).strip()
    for tentativa in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.strptime(s, "%d/%m/%Y"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            dt = tentativa(texto)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    log.debug("Data não reconhecida: %r", valor)
    return None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def criar_sessao() -> requests.Session:
    """Sessão com retry exponencial — as APIs do TCU oscilam, sobretudo à noite."""
    sessao = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adaptador = HTTPAdapter(max_retries=retry, pool_maxsize=10)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "painel-mpo/1.0 (monitoramento de dados abertos)",
        }
    )
    return sessao


def get_json(sessao: requests.Session, url: str, **kwargs: Any) -> Any | None:
    try:
        resp = sessao.get(url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as exc:
        log.warning("HTTP %s em %s", exc.response.status_code if exc.response else "?", url)
    except requests.exceptions.RequestException as exc:
        log.warning("Falha de rede em %s: %s", url, exc)
    except ValueError:
        log.warning("Resposta não-JSON em %s", url)
    return None


# --------------------------------------------------------------------------- #
# Fonte 1 — Solicitações do Congresso Nacional
# --------------------------------------------------------------------------- #


def iter_scn(sessao: requests.Session, max_paginas: int = MAX_PAGINAS_SCN) -> Iterator[dict]:
    """Percorre a paginação do ORDS seguindo a chave 'next'."""
    url: str | None = SCN_URL
    pagina = 0
    while url and pagina < max_paginas:
        dados = get_json(sessao, url)
        if not isinstance(dados, dict):
            break
        itens = dados.get("items") or []
        log.info("SCN página %d: %d itens", pagina, len(itens))
        yield from itens
        proximo = dados.get("next")
        url = proximo.get("$ref") if isinstance(proximo, dict) else None
        if url and url.startswith("http://"):  # o ORDS devolve http; força TLS
            url = "https://" + url[len("http://") :]
        pagina += 1
    if pagina >= max_paginas:
        log.warning("Limite de %d páginas atingido no SCN — pode haver truncamento.", max_paginas)


def coletar_scn(sessao: requests.Session) -> list[dict]:
    resultados: list[dict] = []
    vistos: set[tuple] = set()

    for item in iter_scn(sessao):
        assunto = item.get("assunto") or ""
        marca = classificar(assunto)
        if marca is None:
            continue

        processo = formatar_processo(item.get("processo_scn"))
        chave = (item.get("tipo"), item.get("numero"), processo)
        if chave in vistos:  # a base do TCU tem duplicatas reais
            continue
        vistos.add(chave)

        data = parse_data(item.get("data_aprovacao"))
        resultados.append(
            {
                "fonte": "SCN",
                "orgaos": marca["orgaos"],
                "temas": marca["temas"],
                "confianca": marca["confianca"],
                "processo": processo,
                "processo_id": limpar_processo(processo),
                "tipo": item.get("tipo"),
                "numero": item.get("numero"),
                "assunto": assunto.strip(),
                "autor": (item.get("autor") or "").strip() or None,
                "data": iso(data),
                "link_proposicao": item.get("link_proposicao"),
                "link_tcu": f"{SCN_URL}/{limpar_processo(processo)}" if processo else None,
            }
        )

    nominais = sum(1 for r in resultados if r["confianca"] == "nominal")
    log.info("SCN: %d itens (%d nominais, %d por matéria)", len(resultados), nominais, len(resultados) - nominais)
    return resultados


# --------------------------------------------------------------------------- #
# Fonte 2 — Acórdãos
# --------------------------------------------------------------------------- #


def coletar_acordaos(sessao: requests.Session, maximo: int) -> list[dict]:
    """
    O endpoint devolve um dump sem filtro; a seleção é local sobre título+sumário.
    Fonte instável: se falhar, devolve lista vazia sem derrubar a coleta.
    """
    resultados: list[dict] = []
    inicio = 0

    while inicio < maximo:
        lote = get_json(
            sessao,
            ACORDAOS_URL,
            params={"inicio": inicio, "quantidade": LOTE_ACORDAOS},
        )
        if not isinstance(lote, list):
            log.warning("Acórdãos indisponíveis a partir do índice %d — seguindo sem essa fonte.", inicio)
            break
        if not lote:
            break

        for ac in lote:
            marca = classificar(f"{ac.get('titulo', '')} {ac.get('sumario', '')}")
            if marca is None:
                continue
            resultados.append(
                {
                    "fonte": "Acórdão",
                    "orgaos": marca["orgaos"],
                    "temas": marca["temas"],
                    "confianca": marca["confianca"],
                    "processo": None,
                    "processo_id": None,
                    "numero_acordao": ac.get("numeroAcordao"),
                    "ano": ac.get("anoAcordao"),
                    "colegiado": ac.get("colegiado"),
                    "relator": ac.get("relator"),
                    "situacao": ac.get("situacao"),
                    "assunto": (ac.get("sumario") or ac.get("titulo") or "").strip(),
                    "data": iso(parse_data(ac.get("dataSessao"))),
                    "link_tcu": ac.get("urlAcordao") or ac.get("urlArquivoPDF"),
                }
            )

        inicio += len(lote)
        if len(lote) < LOTE_ACORDAOS:
            break

    log.info("Acórdãos: %d itens atribuídos aos órgãos monitorados", len(resultados))
    return resultados


# --------------------------------------------------------------------------- #
# Fonte 3 — Pautas de sessão (cruzamento)
# --------------------------------------------------------------------------- #


def coletar_pautas(sessao: requests.Session, processos_interesse: set[str]) -> list[dict]:
    """
    A pauta não traz o órgão; o valor está no cruzamento com os processos já
    identificados. Serve de alerta antecipado: julgamento marcado.
    """
    dados = get_json(sessao, PAUTAS_URL)
    if not isinstance(dados, list):
        log.warning("Pautas indisponíveis — seguindo sem essa fonte.")
        return []

    pautas = []
    for p in dados:
        pid = limpar_processo(p.get("numeroProcesso"))
        if pid and pid in processos_interesse:
            pautas.append(
                {
                    "processo": formatar_processo(p.get("numeroProcesso")),
                    "processo_id": pid,
                    "data_sessao": iso(parse_data(p.get("dataSessao"))),
                    "colegiado": p.get("nomeColegiado") or p.get("siglaColegiado"),
                    "relator": p.get("nomeRelator") or p.get("siglaRelator"),
                    "natureza": p.get("naturezaProcesso"),
                    "tipo": p.get("tipoProcesso"),
                }
            )
    log.info("Pautas: %d processos monitorados com sessão marcada", len(pautas))
    return pautas


# --------------------------------------------------------------------------- #
# Montagem do painel
# --------------------------------------------------------------------------- #


def ordenar_por_data(itens: Iterable[dict]) -> list[dict]:
    """Mais recentes primeiro; itens sem data vão para o fim em vez de quebrar."""
    piso = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(itens, key=lambda i: parse_data(i.get("data")) or piso, reverse=True)


def montar_resumo(itens: list[dict]) -> list[dict]:
    resumo = []
    for sigla, cfg in ORGAOS.items():
        do_orgao = [i for i in itens if sigla in i["orgaos"]]
        por_tema: dict[str, int] = {}
        for item in do_orgao:
            for tema in item["temas"] or ["Não classificado"]:
                por_tema[tema] = por_tema.get(tema, 0) + 1
        ultima = ordenar_por_data(do_orgao)[:1]
        resumo.append(
            {
                "orgao": sigla,
                "nome_completo": cfg["nome"],
                "total": len(do_orgao),
                "por_fonte": {
                    f: sum(1 for i in do_orgao if i["fonte"] == f)
                    for f in sorted({i["fonte"] for i in do_orgao})
                },
                "por_tema": dict(sorted(por_tema.items(), key=lambda kv: -kv[1])),
                "data_ultimo_registro": ultima[0]["data"] if ultima else None,
            }
        )
    return resumo


def montar_resumo_temas(itens: list[dict]) -> list[dict]:
    """Visão por matéria, independente de o texto nomear ou não o órgão."""
    saida = []
    for tema in TEMAS:
        do_tema = [i for i in itens if tema in (i["temas"] or [])]
        if not do_tema:
            continue
        saida.append(
            {
                "tema": tema,
                "total": len(do_tema),
                "nominais": sum(1 for i in do_tema if i["confianca"] == "nominal"),
                "data_ultimo_registro": ordenar_por_data(do_tema)[0]["data"],
            }
        )
    return sorted(saida, key=lambda t: -t["total"])


def montar_payload(itens: list[dict], pautas: list[dict], falhas: list[str]) -> dict:
    ordenados = ordenar_por_data(itens)
    agora = datetime.now(timezone.utc)
    return {
        "schema": 3,
        "resumo_temas": montar_resumo_temas(ordenados),
        # Chaves originais preservadas para não quebrar o front atual:
        "resumo": montar_resumo(ordenados),
        "ultimos_andamentos": ordenados[:15],
        "processos_detalhes": ordenados,
        "ultima_atualizacao": agora.astimezone().strftime("%d/%m/%Y às %H:%M"),
        # Novas:
        "ultima_atualizacao_iso": agora.isoformat(),
        "pautas_futuras": sorted(pautas, key=lambda p: p["data_sessao"] or ""),
        "fontes_indisponiveis": falhas,
        "total_itens": len(ordenados),
    }


def salvar_atomico(payload: dict, caminho: str) -> None:
    """Escrita atômica: o site nunca lê um JSON pela metade durante a atualização."""
    destino = os.path.abspath(caminho)
    pasta = os.path.dirname(destino) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, destino)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coleta dados do TCU sobre o MPO e secretarias.")
    parser.add_argument("--saida", default="dados.json")
    parser.add_argument("--sem-acordaos", action="store_true", help="pula a fonte de acórdãos")
    parser.add_argument("--max-acordaos", type=int, default=10_000)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    sessao = criar_sessao()
    falhas: list[str] = []

    itens = coletar_scn(sessao)
    if not itens:
        falhas.append("SCN")

    if not args.sem_acordaos:
        acordaos = coletar_acordaos(sessao, args.max_acordaos)
        if not acordaos:
            falhas.append("Acórdãos")
        itens.extend(acordaos)

    processos = {i["processo_id"] for i in itens if i.get("processo_id")}
    pautas = coletar_pautas(sessao, processos)

    if not itens:
        log.error("Nenhum item coletado — dados.json NÃO foi sobrescrito.")
        return 1

    payload = montar_payload(itens, pautas, falhas)
    salvar_atomico(payload, args.saida)
    log.info("%s gravado: %d itens, %d na pauta.", args.saida, len(itens), len(pautas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
