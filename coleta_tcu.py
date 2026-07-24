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
from datetime import date, datetime, timedelta, timezone
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
PAUTA_PDF_URL = "https://sessoes-portal-ms.apps.tcu.gov.br/api/sessoes/downloadPautaPublicada/{id}"

# Âncora de varredura das pautas publicadas. O id 23240 corresponde ao BTCU de
# 15/05/2026; os ids crescem a cada edição. A cada execução o coletor avança a
# partir do maior id já processado, gravado no próprio dados.json.
# Ancoragem por data: ids observados 22110=05/08/2024, 22495=28/04/2025,
# 23186=20/03/2026, 23240=15/05/2026 — cerca de um id por dia corrido, mas com
# lacunas (o id é compartilhado com outros tipos de documento). Por isso a
# varredura não pode parar no primeiro vazio.
PAUTA_ID_ANCORA = 23240
PAUTA_DATA_ANCORA = date(2026, 5, 15)
PAUTA_MISSES_SEGUIDOS = 45  # tolera lacunas longas antes de desistir

# OPCIONAL e normalmente vazio. A descoberta é automática: todo processo cuja
# unidade jurisdicionada seja do MPO entra sozinho. Use isto apenas para forçar
# a entrada de um processo que a pauta ainda não mencionou.
PROCESSOS_INTERESSE: set[str] = set()

# Peso do vínculo com o órgão, extraído da própria pauta.
PESO_VINCULO = {"principal": 3.0, "compartilhado": 1.8, "interessado": 1.0}

# A pauta é dominada por atos de pessoal e cobranças. São processos legítimos,
# mas não são matéria de gestão do Ministério: entram no painel rebaixados,
# não excluídos, para não esconder nada de quem quiser olhar.
NATUREZAS_ROTINA = (
    r"aposentadoria", r"pens[aã]o (civil|militar)", r"reforma\b",
    r"tomada de contas especial", r"recolhimento administrativo",
    r"admiss[aã]o de pessoal", r"ato de pessoal",
)
# Matérias em que uma decisão do TCU muda a vida do Ministério.
NATUREZAS_ALTA = (
    r"auditoria", r"acompanhament", r"levantament", r"monitorament",
    r"desestatiza[cç][aã]o", r"consulta", r"solicita[cç][aã]o do congresso",
    r"representa[cç][aã]o", r"den[uú]ncia", r"presta[cç][aã]o de contas",
    r"relat[oó]rio de gest[aã]o",
)

# Unidades jurisdicionadas de interesse. Diferente das camadas nominal e
# temática, isto casa contra um campo que o PRÓPRIO TCU declara na pauta —
# é a atribuição de maior confiança que a base pública oferece.
UNIDADES_ALVO: dict[str, list[str]] = {
    "MPO": [
        r"minist[eé]rio do planejamento e or[cç]amento",
        r"secretaria-?executiva do minist[eé]rio do planejamento",
        r"assessoria especial de controle interno do minist[eé]rio do planejamento",
    ],
    "SOF": [r"secretaria de or[cç]amento federal"],
    "SEPLAN": [r"secretaria nacional de planejamento"],
    "SMA": [r"secretaria (nacional )?de monitoramento e avalia[cç][aã]o"],
}

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
    Só entra no painel quem NOMEIA o órgão.

    A camada temática (LDO, emendas, transferências) foi rebaixada a etiqueta.
    Ela dava entrada e enchia o painel de processos do Ministério da Saúde, do
    DNOCS, de cisternas — matéria orçamentária de terceiros, não do MPO. Tema
    descreve o assunto; não estabelece vínculo com o Ministério.

    Devolve None quando nenhum órgão monitorado é citado.
    """
    norm = normalizar(texto)
    orgaos = identificar(norm, RX_ORGAOS)
    if not orgaos:
        return None
    return {"orgaos": orgaos, "temas": identificar(norm, RX_TEMAS), "confianca": "nominal"}


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


def indexar_scn(sessao: requests.Session) -> dict[str, dict]:
    """
    Índice processo -> solicitação do Congresso, para ENRIQUECER processos já
    identificados como do MPO por outra via.

    O SCN não serve para descobrir: o assunto descreve o que o parlamentar pediu
    ("auditoria na LDO 2026"), quase nunca nomeia a pasta responsável. Casar por
    texto trazia dezenas de processos de outros ministérios. Casar pelo NÚMERO do
    processo é exato: se a pauta diz que 017.443/2025-3 é do MPO e o SCN diz que
    ele nasceu de um requerimento do deputado X, os dois falam do mesmo processo.
    """
    indice: dict[str, dict] = {}
    for item in iter_scn(sessao):
        pid = limpar_processo(item.get("processo_scn"))
        if not pid:
            continue
        indice[pid] = {
            "origem_congresso": {
                "tipo": item.get("tipo"),
                "numero": item.get("numero"),
                "autor": (item.get("autor") or "").strip() or None,
                "assunto": (item.get("assunto") or "").strip(),
                "data": iso(parse_data(item.get("data_aprovacao"))),
                "link_proposicao": item.get("link_proposicao"),
            }
        }
    log.info("SCN: %d solicitações indexadas para cruzamento por número", len(indice))
    return indice


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
# Fonte 4 — Pautas publicadas (BTCU) — a fonte de maior confiança
# --------------------------------------------------------------------------- #

RX_UNIDADES = _compilar(UNIDADES_ALVO)

# Cabeçalhos que mudam o contexto corrente enquanto varremos o documento.
RX_RELATOR = re.compile(r"^\s*(?:Ministro|MINISTRO)(?:-Substituto|-SUBSTITUTO)?\s+([A-ZÁÂÃÉÊÍÓÔÕÚÇ][A-Za-zÁÂÃÉÊÍÓÔÕÚÇáâãéêíóôõúç\s\.]{4,60})\s*$")
RX_COLEGIADO = re.compile(r"PAUTA (?:DO|DA) (PLEN[ÁA]RIO|PRIMEIRA C[ÂA]MARA|SEGUNDA C[ÂA]MARA)")

# O BTCU é dividido em seções, e a seção diz o ESTADO do processo. Um processo
# nas Pautas ainda vai a julgamento; nas Atas já foi julgado e gerou acórdão.
# Antes eu lia só as Pautas — por isso o painel só mostrava o que estava por vir.
RX_SECAO = re.compile(
    r"^\s*(PAUTAS?|ATAS?|DESPACHOS DE AUTORIDADES|EDITAIS|"
    r"ACORD[ÃA]OS|DELIBERA[ÇC][ÕO]ES)\s*$", re.I)
SECAO_STATUS = {
    "pauta": ("Em pauta", "Aguardando julgamento em sessão marcada"),
    "ata": ("Julgado", "Deliberado em sessão; acórdão proferido"),
    "despacho": ("Despacho", "Decisão monocrática do relator"),
    "edital": ("Edital", "Citação, audiência ou notificação publicada"),
    "indefinido": ("Registrado", "Mencionado no boletim, fase não identificada"),
}
# Número do acórdão dentro das atas: "ACÓRDÃO Nº 1.234/2026 - TCU - Plenário"
RX_ACORDAO = re.compile(
    r"AC[ÓO]RD[ÃA]O\s+N?[ºo°]?\s*([\d.]+)\s*/\s*(\d{4})\s*[-–]\s*TCU\s*[-–]\s*"
    r"(Plen[áa]rio|Primeira C[âa]mara|Segunda C[âa]mara|1[ªa] C[âa]mara|2[ªa] C[âa]mara)", re.I)


def _secao_de(titulo: str) -> str:
    t = normalizar(titulo)
    if t.startswith("pauta"): return "pauta"
    if t.startswith("ata"): return "ata"
    if "despacho" in t: return "despacho"
    if "edital" in t: return "edital"
    if "acorda" in t or "delibera" in t: return "ata"
    return "indefinido"
RX_SESSAO = re.compile(r"Sess[ãa]o\s+\w+\s+de\s+(\d{2}/\d{2}/\d{4})")
# Início de bloco de processo. O número aparece em várias roupagens ao longo do
# boletim, e exigir "número no começo da linha seguido de hífen" perdia TODAS as
# relações — que é onde os acórdãos são publicados em lote:
#     022.756/2025-6 - Auditoria...        (pauta)
#     1. Processo TC-022.852/2025-5        (relação)
#     2. Processo TC 005.405/2026-2 - ...  (relação, com espaço)
#     Anexo: TC-022.280/2024-3 - ...       (apensado)
RX_BLOCO = re.compile(
    r"^\s*(?:\d{1,3}\s*[.)]\s*)?"                    # "1." da relação
    r"(?:(?:Processo|Anexo|Apenso|Apensos?)\s*:?\s*)?"  # rótulo
    r"(?:TC[-\s]\s*)?"                                 # prefixo TC
    r"(\d{3}\.\d{3}/\d{4}-\d)"
    r"\s*-?\s*", re.M)
# Rótulos que encerram a descrição e iniciam campos estruturados.
RX_CAMPO = re.compile(
    r"(Natureza|Unidade [Jj]urisdicionada|[ÓO]rg[ãa]o/Entidade/Unidade|[ÓO]rg[ãa]o/Entidade|"
    r"Respons[áa]ve(?:l|is)|Interessad[oa]s?|Representa[çc][ãa]o legal|Recorrentes?|"
    r"Embargantes?|Representante|Solicitante|Exerc[íi]cio|Revisor|Interesse em sustenta[çc][ãa]o oral)\s*:",
)
RX_RUIDO = re.compile(
    r"(Para verificar as assinaturas.*?\d{8}\.|BTCU Deliberações.*?\d{4}\s+\d+|"
    r"CODMATERIA=\d+|A presente pauta pode.*?RITCU\)\.|As transmiss[õo]es das sess[õo]es.*?sessoes/\.)",
    re.S,
)


def _limpar_pagina(texto: str) -> str:
    """Remove rodapés e boilerplate que se repetem a cada página do boletim."""
    texto = RX_RUIDO.sub(" ", texto)
    return re.sub(r"[ \t]+", " ", texto)


def _extrair_campo(bloco: str, rotulos: tuple[str, ...]) -> str | None:
    """Devolve o valor de um rótulo até o próximo rótulo estruturado."""
    for rotulo in rotulos:
        m = re.search(rotulo + r"\s*:\s*(.+)", bloco, re.S)
        if not m:
            continue
        resto = m.group(1)
        fim = RX_CAMPO.search(resto)
        valor = (resto[: fim.start()] if fim else resto).strip()
        valor = re.sub(r"\s+", " ", valor).rstrip(".").strip()
        if valor and valor.lower() not in {"não há", "nao ha"}:
            return valor
    return None


RX_ROTINA = [re.compile(p) for p in NATUREZAS_ROTINA]
RX_ALTA = [re.compile(p) for p in NATUREZAS_ALTA]


def classe_materia(natureza: str | None, descricao: str) -> tuple[str, float]:
    """Separa matéria de gestão de rotina administrativa, sem descartar nada."""
    texto = normalizar(f"{natureza or ''} {descricao[:160]}")
    if any(rx.search(texto) for rx in RX_ROTINA):
        return "rotina", 0.25
    if any(rx.search(texto) for rx in RX_ALTA):
        return "materia", 1.0
    return "outra", 0.6


def grau_vinculo(unidade: str | None, interessados: str | None,
                 regras: dict[str, list[re.Pattern[str]]]) -> tuple[list[str], str]:
    """
    Quão diretamente o órgão está preso ao processo. Ser a única unidade
    jurisdicionada não é o mesmo que aparecer como um entre sete interessados.
    """
    na_unidade = identificar(normalizar(unidade or ""), regras)
    if na_unidade:
        vinculo = "compartilhado" if ";" in (unidade or "") else "principal"
        return na_unidade, vinculo
    nos_interessados = identificar(normalizar(interessados or ""), regras)
    if nos_interessados:
        return nos_interessados, "interessado"
    return [], "nenhum"


def exigir_pypdf():
    """
    A ausência do pypdf fazia TODA a fonte de pautas sumir silenciosamente,
    aparecendo no painel como "não respondeu" — um diagnóstico errado que custa
    tempo. Agora a falta da dependência para a execução com a mensagem certa.
    """
    try:
        from pypdf import PdfReader
        return PdfReader
    except ImportError:
        raise SystemExit(
            "\nFALTA DEPENDÊNCIA: pypdf\n"
            "As pautas do TCU são PDF. Instale com:\n"
            "    pip install pypdf\n"
            "e confirme que 'pypdf' está no requirements.txt do repositório.\n"
        )


def baixar_pauta(sessao: requests.Session, id_pauta: int) -> str | None:
    """Baixa uma edição do BTCU e devolve o texto. None se a edição não existe."""
    try:
        resp = sessao.get(PAUTA_PDF_URL.format(id=id_pauta), timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.content[:5].startswith(b"%PDF"):
            return None
    except requests.exceptions.RequestException as exc:
        log.debug("Pauta %s indisponível: %s", id_pauta, exc)
        return None

    import io

    try:
        leitor = exigir_pypdf()(io.BytesIO(resp.content))
        return "\n".join(p.extract_text() or "" for p in leitor.pages)
    except Exception as exc:  # PDF corrompido não deve derrubar a coleta
        log.warning("Falha ao ler a pauta %s: %s", id_pauta, exc)
        return None


def parsear_pauta(texto: str, id_pauta: int) -> list[dict]:
    """
    Percorre o boletim mantendo o contexto corrente (colegiado, data da sessão,
    relator) e emite um registro por processo cuja unidade jurisdicionada — ou
    cujo número — seja de interesse.
    """
    texto = _limpar_pagina(texto)
    linhas = texto.split("\n")

    colegiado = data_sessao = relator = None
    secao = "indefinido"
    acordao_atual: str | None = None
    registros: list[dict] = []
    buffer: list[str] = []
    numero_atual: str | None = None

    def fechar() -> None:
        nonlocal buffer, numero_atual
        if not numero_atual:
            buffer = []
            return
        bloco = " ".join(buffer)
        unidade = _extrair_campo(
            bloco, (r"Unidade [Jj]urisdicionada", r"[ÓO]rg[ãa]o/Entidade/Unidade", r"[ÓO]rg[ãa]o/Entidade")
        )
        interessados = _extrair_campo(bloco, (r"Interessad[oa]s?",))
        orgaos, vinculo = grau_vinculo(unidade, interessados, RX_UNIDADES)
        na_watchlist = numero_atual in PROCESSOS_INTERESSE

        if orgaos or na_watchlist:
            corte = RX_CAMPO.search(bloco)
            descricao = (bloco[: corte.start()] if corte else bloco).strip().rstrip(".")
            natureza = _extrair_campo(bloco, (r"Natureza",))
            classe, peso_materia = classe_materia(natureza, descricao)
            relevancia = round(PESO_VINCULO.get(vinculo, 1.0) * peso_materia, 2)
            status, status_desc = SECAO_STATUS[secao]
            # O número do acórdão pode estar no próprio bloco ou no cabeçalho da relação.
            m_ac = RX_ACORDAO.search(bloco)
            acordao = f"{m_ac.group(1)}/{m_ac.group(2)}-{m_ac.group(3)}" if m_ac else (
                acordao_atual if secao == "ata" else None)
            registros.append(
                {
                    "fonte": "Pauta" if secao == "pauta" else "Acórdão" if secao == "ata" else "Boletim",
                    "secao": secao,
                    "status": status,
                    "status_descricao": status_desc,
                    "acordao": acordao,
                    "orgaos": orgaos,
                    "temas": identificar(normalizar(descricao), RX_TEMAS),
                    "confianca": "unidade_jurisdicionada" if orgaos else "watchlist",
                    "vinculo": vinculo,
                    "classe": classe,
                    "relevancia": relevancia,
                    "processo": numero_atual,
                    "processo_id": limpar_processo(numero_atual),
                    "assunto": descricao or "Processo incluído em pauta",
                    "unidade_jurisdicionada": unidade,
                    "relator": relator,
                    "colegiado": colegiado,
                    "natureza": natureza,
                    "data": iso(parse_data(data_sessao)),
                    "data_sessao": iso(parse_data(data_sessao)) if secao == "pauta" else None,
                    "data_julgamento": iso(parse_data(data_sessao)) if secao == "ata" else None,
                    "edicao_btcu": id_pauta,
                    "link_tcu": None,
                }
            )
        buffer = []
        numero_atual = None

    for linha in linhas:
        if m := RX_SECAO.match(linha):
            fechar()
            secao = _secao_de(m.group(1))
            continue
        if m := RX_ACORDAO.search(linha):
            # O cabeçalho vem ANTES do processo que ele decide. Fechar o bloco
            # pendente primeiro, senão o processo anterior herda o acórdão errado.
            fechar()
            acordao_atual = f"{m.group(1)}/{m.group(2)}-{m.group(3)}"
            if secao == "indefinido":
                secao = "ata"
        if m := RX_COLEGIADO.search(linha):
            fechar()
            colegiado = m.group(1).title().replace("Camara", "Câmara")
            secao = "pauta"   # "PAUTA DO PLENÁRIO" abre a seção, não só o título "PAUTAS"
            continue
        if m := RX_SESSAO.search(linha):
            data_sessao = m.group(1)
            continue
        if m := RX_RELATOR.match(linha):
            fechar()
            relator = m.group(1).strip().title()
            continue
        if m := RX_BLOCO.match(linha):
            fechar()
            numero_atual = m.group(1)
            buffer = [linha[m.end():]]
            continue
        if numero_atual is not None:
            buffer.append(linha)

    fechar()
    return registros


def coletar_pautas_publicadas(sessao: requests.Session, ancora: int, maximo: int) -> tuple[list[dict], int]:
    """
    Avança a partir da âncora até acumular MISSES seguidos. Devolve os registros
    e o maior id efetivamente lido, para servir de âncora na próxima execução.
    """
    # Teto estimado: ~1,1 id por dia desde a âncora conhecida, com folga.
    dias = (date.today() - PAUTA_DATA_ANCORA).days
    teto = max(ancora, PAUTA_ID_ANCORA) + int(dias * 1.1) + 40
    log.info("Varrendo pautas de %d até no máximo %d", ancora, teto)

    registros: list[dict] = []
    misses = 0
    maior = ancora
    id_atual = ancora
    lidas = 0

    while misses < PAUTA_MISSES_SEGUIDOS and lidas < maximo and id_atual <= teto:
        texto = baixar_pauta(sessao, id_atual)
        if texto is None:
            misses += 1
        else:
            misses = 0
            maior = max(maior, id_atual)
            lidas += 1
            achados = parsear_pauta(texto, id_atual)
            if achados:
                log.info("Pauta %s: %d processos de interesse", id_atual, len(achados))
            registros.extend(achados)
        id_atual += 1

    log.info("Pautas: %d edições lidas, %d registros, âncora agora em %d", lidas, len(registros), maior)
    return registros, maior


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


def montar_watchlist(itens: list[dict]) -> list[dict]:
    """Situação de cada processo da lista de acompanhamento nominal."""
    saida = []
    for numero in sorted(PROCESSOS_INTERESSE):
        eventos = [i for i in itens if i.get("processo") == numero]
        eventos = ordenar_por_data(eventos)
        saida.append({
            "processo": numero,
            "processo_id": limpar_processo(numero),
            "eventos": len(eventos),
            "ultimo": eventos[0] if eventos else None,
        })
    return saida


def montar_resumo_status(itens: list[dict]) -> list[dict]:
    """Quantos processos DISTINTOS em cada fase. Contar registros inflaria o número:
    o mesmo processo aparece em várias edições do boletim."""
    ordem = ["Em pauta", "Julgado", "Despacho", "Edital", "Registrado"]
    saida = []
    for status in ordem:
        procs = {i["processo"] for i in itens if i.get("status") == status and i.get("processo")}
        if procs:
            saida.append({"status": status, "processos": len(procs),
                          "registros": sum(1 for i in itens if i.get("status") == status)})
    return saida


def montar_payload(itens: list[dict], pautas: list[dict], falhas: list[str],
                   ancora_pauta: int = PAUTA_ID_ANCORA) -> dict:
    ordenados = ordenar_por_data(itens)
    agora = datetime.now(timezone.utc)
    return {
        "schema": 5,
        "resumo_status": montar_resumo_status(ordenados),
        "ancora_pauta": ancora_pauta,
        "watchlist": montar_watchlist(ordenados),
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
    parser.add_argument("--sem-pautas", action="store_true", help="pula as pautas publicadas")
    parser.add_argument("--max-acordaos", type=int, default=10_000)
    parser.add_argument("--max-pautas", type=int, default=120,
                        help="máximo de edições do BTCU lidas por execução")
    parser.add_argument("--desde-id", type=int, default=None,
                        help="força o início da varredura neste id, ignorando a âncora salva. "
                             "Use na primeira carga para recuar anos: 22110 ≈ ago/2024, "
                             "22495 ≈ abr/2025, 23186 ≈ mar/2026, 23240 ≈ mai/2026.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    sessao = criar_sessao()
    falhas: list[str] = []

    # A âncora das pautas vem do dados.json anterior: cada execução retoma de
    # onde a última parou, em vez de revarrer o boletim inteiro.
    if args.desde_id:
        ancora = args.desde_id
        log.info("Carga forçada a partir do id %d (--desde-id)", ancora)
    else:
        ancora = PAUTA_ID_ANCORA
        try:
            with open(args.saida, encoding="utf-8") as f:
                ancora = max(ancora, int(json.load(f).get("ancora_pauta", ancora)))
            log.info("Retomando a varredura de pautas do id %d", ancora)
        except (OSError, ValueError, TypeError):
            log.info("Sem âncora anterior; começando do id %d", ancora)

    # Descoberta: quem é do MPO vem do boletim, onde o TCU declara a unidade
    # jurisdicionada. O SCN entra depois, só para dizer de onde o processo veio.
    itens: list[dict] = []
    if not args.sem_pautas:
        itens, ancora = coletar_pautas_publicadas(sessao, ancora, args.max_pautas)
        if not itens:
            falhas.append("Boletim do TCU")

    indice_scn = indexar_scn(sessao)
    if not indice_scn:
        falhas.append("SCN")
    else:
        casados = 0
        for item in itens:
            extra = indice_scn.get(item.get("processo_id") or "")
            if extra:
                item.update(extra)
                casados += 1
        log.info("SCN: %d processos enriquecidos com a origem no Congresso", casados)

    if not args.sem_acordaos:
        acordaos = coletar_acordaos(sessao, args.max_acordaos)
        if not acordaos:
            falhas.append("Acórdãos")
        itens.extend(acordaos)

    processos = {i["processo_id"] for i in itens if i.get("processo_id")}
    pautas = coletar_pautas(sessao, processos)

    if not itens:
        if os.path.exists(args.saida):
            log.warning(
                "Nenhum item coletado nesta execução. O %s anterior foi preservado "
                "e o painel segue no ar com os dados da última coleta bem-sucedida.",
                args.saida,
            )
            return 0
        log.error(
            "Nenhum item coletado e não existe %s anterior. Verifique o log acima: "
            "se todas as fontes falharam, é indisponibilidade do TCU; se só as pautas "
            "falharam, confirme que o pypdf está instalado.",
            args.saida,
        )
        return 1

    payload = montar_payload(itens, pautas, falhas, ancora)
    salvar_atomico(payload, args.saida)
    log.info("%s gravado: %d itens, %d na pauta.", args.saida, len(itens), len(pautas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
