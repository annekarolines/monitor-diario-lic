#!/usr/bin/env python3
"""
Licitações de Comunicação — Coleta diária
Fontes:
  1. PNCP  — Portal Nacional de Contratações Públicas (pncp.gov.br)
  2. Portal da Transparência — api.portaldatransparencia.gov.br
  3. DOU   — Diário Oficial da União, Seção 3 (in.gov.br)
  4. QD    — Querido Diário, diários oficiais municipais (queridodiario.ok.org.br)
  5. DODF  — Diário Oficial do Distrito Federal (dodf.df.gov.br)

Limites Portal da Transparência:
  - 00h-06h BRT: 700 req/min
  - Demais horários: 400 req/min
  - APIs restritas: 180 req/min
  Usamos 120 req/min como margem segura (abaixo do limite restrito).

# ---------------------------------------------------------------------------
# Fontes planejadas (não implementadas)
# ---------------------------------------------------------------------------
#
# 6. Google Notícias RSS — esforço estimado: baixo
#    Fallback para portais fora do escopo das APIs (Sistema S, Diários estaduais
#    não cobertos pelo QD, portais regionais).
#    URL: https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR&ceid=BR:pt-419
#    Queries sugeridas: "licitação agência publicidade", "pregão comunicação marketing",
#                       "concorrência publicidade propaganda"
#    Prós: cobertura ampla, feed público sem autenticação, fácil de parsear (feedparser)
#    Contras: resultados são notícias sobre licitações (já publicadas em veículos de
#             imprensa), não editais diretos — score Gemini filtraria os falsos positivos
#
# 7. Portal FIEG / Sistema S estadual — esforço estimado: alto
#    Licitações do SENAI, SESI e SESC publicadas nas federações industriais estaduais.
#    Entidades do Sistema S não são regidas pela Lei 14.133/2021 — não publicam no PNCP
#    e não precisam publicar no DOU, apenas em seu próprio portal.
#    Portais por estado (todos sem API pública):
#      FIEG (GO): https://www.fieg.com.br/licitacao/           ← caso SENAI/SESI Goiás
#      FIESP (SP): https://www.fiesp.com.br/licitacoes/
#      FIEMG (MG): https://www7.fiemg.com.br/licitacoes/
#      FIERGS (RS): https://www.fiergs.org.br/licitacoes
#      FIERJ (RJ): https://www.firjan.com.br/licitacoes/
#    Todos são aplicações legadas (Java/PHP) com sessão HTML — requerem scraping.
#    Recomendação: priorizar FIEG (Goiás) e FIESP (São Paulo) na primeira iteração.
"""

import os
import json
import hashlib
import time
import re
import math
import unicodedata
from collections import defaultdict
import requests
from google import genai
from google.genai import types as genai_types
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

_gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = "gemini-2.5-flash-lite"

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GEMINI_DELAY         = 5    # segundos entre chamadas Gemini (free tier: 20 RPM)
# Quotas por fonte: garante que DOU/DODF nunca sejam descartados por volume de PNCP.
# Total = 50 análises/run — bem abaixo do limite free tier (1.500 RPD, 20 RPM).
GEMINI_SOURCE_QUOTAS = {
    "pncp":         20,
    "transparencia": 5,
    "dou":          10,
    "dodf":          8,
    "qd":            7,
}  # total = 50
MIN_VALOR            = 10_000.0
MAX_AGE_DAYS         = 30
PAGE_SIZE            = 50   # máximo PNCP

# PNCP
PNCP_BASE = "https://pncp.gov.br/pncp-consulta/v1/contratacoes/publicacao"
# Modalidades: 3=Concurso, 4=Concorrência Eletrônica, 5=Concorrência Presencial,
#              6=Pregão Eletrônico, 7=Pregão Presencial, 8=Dispensa, 9=Inexigibilidade
MODALIDADES = [4, 5, 6, 7, 8, 9, 3]

# Portal da Transparência
TRANSPARENCIA_BASE      = "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes"
TRANSPARENCIA_PAGE_SIZE = 500   # máximo aceito pela API
TRANSPARENCIA_RPM       = 120   # conservador: abaixo do limite de 180 (APIs restritas)

# DOU — Diário Oficial da União
DOU_BASE = "https://www.in.gov.br/leiturajornal"
# artType values que indicam NOVA licitação/edital no DO3
DOU_ART_TYPES = {
    "aviso de licitação",
    "aviso de licitação pública",
    "aviso de pregão eletrônico",
    "aviso de pregão presencial",
    "aviso de concorrência",
    "aviso de dispensa eletrônica",
    "aviso de dispensa de licitação",
    "aviso de chamamento público",
    "aviso de inexigibilidade",
    "aviso de credenciamento",
    "aviso",
}
# artTypes que NÃO são novas oportunidades: suspensões, anulações, extratos, ARPs já assinadas
DOU_ART_TYPES_EXCLUDE = {
    "aviso de suspensão",
    "aviso de suspensao",
    "aviso de reabertura",          # reabertura já está no sistema
    "aviso de intenção de anulação",
    "aviso de intencao de anulacao",
    "extrato de termo aditivo",
    "extrato de contrato",
    "extrato",
    "aviso de registro de preços",  # ARP já assinada, não é abertura
    "aviso de registro de precos",
    "resultado de julgamento",
    "resultado de licitação",
}
# Frases de início de artigo que indicam NÃO ser uma nova licitação
DOU_CONTENT_EXCLUDE_PREFIXES = (
    "extrato de termo aditivo",
    "extrato de contrato",
    "resultado de julgamento",
    "homologação",
    "adjudicação",
    "revogação",
    "anulação",
)

# Querido Diário — diários oficiais municipais
QD_BASE                  = "https://api.queridodiario.ok.org.br/gazettes"
QD_PAGE_SIZE             = 50
QD_EXCERPT_SIZE          = 800
QD_NUMBER_OF_EXCERPTS    = 3
QD_MAX_RESULTS_PER_CLUSTER = 200   # cap: 4 páginas de 50 por cluster

# 3 clusters temáticos para cobrir os KEYWORDS sem URL excessivamente longa
QD_QUERY_CLUSTERS = [
    (
        '"agência de publicidade" OR "agencia de publicidade"'
        ' OR "agência de propaganda" OR "agencia de propaganda"'
        ' OR publicidade OR propaganda'
    ),
    (
        '"marketing digital" OR "redes sociais" OR "mídias sociais"'
        ' OR "midias sociais" OR "comunicação digital" OR "comunicacao digital"'
        ' OR "gestão de redes" OR "gestao de redes"'
    ),
    (
        '"campanha publicitária" OR "campanha publicitaria"'
        ' OR "identidade visual" OR "conteúdo digital" OR "conteudo digital"'
        ' OR "publicidade institucional" OR "comunicação institucional"'
        ' OR "comunicacao institucional"'
    ),
]

# DODF — Diário Oficial do Distrito Federal
DODF_BASE         = "https://dodf.df.gov.br/dodf/jornal/diario"
DODF_MATERIA_URL  = "https://dodf.df.gov.br/dodf/materia/visualizar"
DODF_ITEMS_PER_PAGE = 10    # fixo pelo portal
DODF_MAX_PAGES    = 30      # cap por dia (~300 matérias) para limitar requests
# tipos de matéria no DODF que indicam licitação/contratação
DODF_TIPOS_RELEVANTES = {
    "aviso",
    "aviso de abertura de licitação",
    "aviso de abertura",
    "edital",
    "aviso de suspensão",
    "aviso de reabertura",
}

DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "licitacoes.json")

# ---------------------------------------------------------------------------
# Palavras-chave
# ---------------------------------------------------------------------------

KEYWORDS = [
    "agência de publicidade", "agencia de publicidade",
    "agência de propaganda", "agencia de propaganda",
    "propaganda",
    "publicidade",
    "comunicação digital", "comunicacao digital",
    "marketing digital",
    "redes sociais", "mídias sociais", "midias sociais",
    "gestão de redes", "gestao de redes",
    "produção de conteúdo", "producao de conteudo",
    "conteúdo digital", "conteudo digital",
    "campanha publicitária", "campanha publicitaria",
    "identidade visual",
    "criação de campanha", "criacao de campanha",
    "publicidade institucional",
    "comunicação institucional", "comunicacao institucional",
    "serviços de publicidade", "servicos de publicidade",
    "mídias digitais", "midias digitais",
]

EXCLUDE_KEYWORDS = [
    # Áreas não relacionadas a agência de comunicação
    "assessoria de imprensa",
    "assessoria à imprensa",
    "fornecimento de equipamento",
    "equipamentos de informática",
    "equipamentos de informatica",
    "radiocomunicação",
    "radiocomunicacao",
    "sistema de comunicacao de dados",
    "sistema de comunicação de dados",
    # Atos regulatórios e administrativos (CADE, etc.)
    "ato de concentracao",
    "ato de concentração",
    "da-se publicidade ao seguinte ato",
    "dá-se publicidade ao seguinte ato",
    # Usos de "publicidade" como princípio jurídico/transparência — NÃO é agência
    "principio da publicidade",
    "princípio da publicidade",
    "publicidade e transparencia",
    "publicidade e transparência",
    "publicidade legal",          # publicação de avisos legais no DOU/jornal
    "publicidade dos atos",       # publicação de atos oficiais
    "publicidade de atos",
    "publicidade institucional dos atos",
    "em cumprimento ao principio",
    # Credenciamento de pessoas físicas (júri, avaliação) — não é contrato de agência
    "pessoas fisicas",
    "pessoa fisica",
    "subcomissao de julgamento",
    "subcomissão de julgamento",
]

# ---------------------------------------------------------------------------
# Prompts Gemini
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um analista de inteligência de licitações especializado em serviços de comunicação e marketing.
Você apoia uma diretora de inteligência de agência de comunicação que identifica oportunidades de negócio
em licitações públicas brasileiras. Responda sempre em Português do Brasil, de forma objetiva e estratégica."""

ANALYSIS_PROMPT = """Analise esta licitação pública e retorne um JSON válido indicando se é oportunidade para uma agência de comunicação.

Dados da licitação:
- Órgão: {orgao}
- Objeto: {objeto}
- Modalidade: {modalidade}
- Valor estimado: {valor}
- Data de publicação: {data}

Estrutura exata do JSON:
{{
  "relevante": true,
  "categoria": "Marketing Digital",
  "objeto_resumido": "descrição concisa do serviço em 1-2 frases",
  "justificativa": "1 frase estratégica sobre por que é oportunidade para agência",
  "score_relevancia": 8
}}

Regras:
- "relevante": true se o objeto envolver publicidade, marketing digital, redes sociais, conteúdo criativo, identidade visual ou comunicação institucional. false se for equipamentos, assessoria de imprensa sem publicidade, radiocomunicação, ou fora do escopo de comunicação criativa.
- "categoria": exatamente uma de: "Publicidade & Propaganda" | "Marketing Digital" | "Conteúdo & Redes Sociais" | "Identidade Visual & Criação" | "Comunicação Institucional"
- "objeto_resumido": máximo 2 frases, linguagem clara, sem juridiquês
- "justificativa": máximo 1 frase, ângulo estratégico (porte do contrato, escopo, perfil do órgão)
- "score_relevancia": 1-10 onde:
    9-10 = valor > R$500k, escopo amplo (publicidade + digital + conteúdo)
    7-8  = valor > R$100k, escopo claro em comunicação digital ou publicidade
    5-6  = valor < R$100k ou escopo parcial (só redes, só criação pontual)
    1-4  = valor baixo, escopo restrito, ou serviço muito específico

Responda APENAS com o JSON, sem texto adicional, sem blocos de código."""


# ---------------------------------------------------------------------------
# Rate Limiter (Portal da Transparência)
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Controla requisições por minuto respeitando os limites da API.
    Usa intervalo mínimo entre chamadas para distribuir as requisições
    uniformemente e nunca ultrapassar o limite configurado.
    """
    def __init__(self, rpm: int):
        self._interval = 60.0 / rpm   # segundos mínimos entre chamadas
        self._last_call = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self._last_call
        remaining = self._interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.time()


transp_limiter = RateLimiter(TRANSPARENCIA_RPM)


# ---------------------------------------------------------------------------
# Helpers gerais
# ---------------------------------------------------------------------------

def load_existing_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_updated": None, "total": 0, "licitacoes": []}


def save_data(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    data["total"] = len(data.get("licitacoes", []))
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def licitacao_id(url):
    return hashlib.md5(url.encode()).hexdigest()


def normalize_text(text):
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def keyword_match(objeto):
    norm = normalize_text(objeto)
    if any(normalize_text(kw) in norm for kw in EXCLUDE_KEYWORDS):
        return False
    return any(normalize_text(kw) in norm for kw in KEYWORDS)


def format_valor(valor):
    if valor is None:
        return "não informado"
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "não informado"


def _iso_from_br(date_str: str) -> str:
    """Converte 'dd/MM/yyyy ...' → 'yyyy-MM-dd'. Retorna '' se inválido."""
    if not date_str:
        return ""
    try:
        if "/" in date_str[:5]:
            parts = date_str[:10].split("/")
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return date_str[:10]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# PNCP — helpers e fetch
# ---------------------------------------------------------------------------

def pncp_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def parse_ambito(item):
    orgao = item.get("orgaoEntidade", {})
    esfera = orgao.get("esferaNome", "").strip()
    uf = (item.get("unidadeOrgao", {}).get("ufSigla", "") or
          orgao.get("ufSigla", "")).strip().upper()
    municipio = (item.get("unidadeOrgao", {}).get("municipioNome", "") or
                 orgao.get("municipioNome", "")).strip()

    esfera_lower = esfera.lower()
    if "federal" in esfera_lower:
        return "Federal"
    elif "estadual" in esfera_lower or "distrital" in esfera_lower:
        return f"Estadual – {uf}" if uf else "Estadual"
    elif "municipal" in esfera_lower:
        if municipio and uf:
            return f"Municipal – {municipio}/{uf}"
        elif uf:
            return f"Municipal – {uf}"
        return "Municipal"
    if municipio and uf:
        return f"Municipal – {municipio}/{uf}"
    return esfera or "Não informado"


def build_pncp_url(item):
    link_origem = (item.get("linkSistemaOrigem") or "").strip()
    if link_origem:
        return link_origem
    cnpj = item.get("orgaoEntidade", {}).get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
    ano  = item.get("anoCompra") or (item.get("dataPublicacaoPncp", "")[:4])
    seq  = item.get("sequencialCompra", "")
    if cnpj and ano and seq:
        return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"
    return "https://pncp.gov.br/app/editais"


def fetch_pncp_modalidade(data_ini: str, data_fim: str, modalidade: int) -> list:
    all_items = []
    pagina = 1
    while True:
        params = {
            "dataInicial": data_ini,
            "dataFinal":   data_fim,
            "codigoModalidadeContratacao": modalidade,
            "pagina":      pagina,
            "tamanhoPagina": PAGE_SIZE,
        }
        try:
            resp = requests.get(PNCP_BASE, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.HTTPError as e:
            if resp.status_code in (404, 422):
                break
            print(f"   Erro HTTP modalidade {modalidade} pág {pagina}: {e}")
            break
        except Exception as e:
            print(f"   Erro modalidade {modalidade} pág {pagina}: {e}")
            break

        items = payload if isinstance(payload, list) else payload.get("data", payload.get("content", []))
        total_pages = 1
        if isinstance(payload, dict):
            total_reg = payload.get("totalRegistros", 0)
            if total_reg and PAGE_SIZE:
                total_pages = math.ceil(total_reg / PAGE_SIZE)

        all_items.extend(items)
        if pagina >= total_pages or len(items) < PAGE_SIZE:
            break
        pagina += 1

    return all_items


def fetch_pncp(data_ini: str, data_fim: str) -> list:
    all_items = []
    for cod in MODALIDADES:
        items = fetch_pncp_modalidade(data_ini, data_fim, cod)
        print(f"   Modalidade {cod}: {len(items)} licitações")
        all_items.extend(items)
    return all_items


# ---------------------------------------------------------------------------
# Portal da Transparência — normalização e fetch
# ---------------------------------------------------------------------------

def transparencia_date(d: date) -> str:
    """Converte date para 'dd/MM/yyyy' exigido pela API da Transparência."""
    return d.strftime("%d/%m/%Y")


def normalize_transparencia_item(item: dict) -> dict:
    """
    Converte um item do Portal da Transparência (schema real) para o formato
    interno compatível com as demais funções do script.

    Schema real da API (LicitacaoDTO):
      item.licitacao.objeto        → descrição do objeto
      item.unidadeGestora.nome     → nome do órgão
      item.modalidadeLicitacao     → modalidade (string direta)
      item.valor                   → valor estimado
      item.dataPublicacao          → data publicação (YYYY-MM-DD)
      item.dataAbertura            → data abertura/prazo (YYYY-MM-DD)
      item.municipio.uf.sigla      → UF
      item.municipio.nomeIBGE      → nome do município
      item.id                      → ID para montar URL
    """
    # Órgão
    orgao_nome = (
        item.get("unidadeGestora", {}).get("nome") or
        item.get("unidadeGestora", {}).get("orgaoVinculado", {}).get("nome") or
        item.get("unidadeGestora", {}).get("orgaoMaximo", {}).get("nome") or
        "Órgão não informado"
    )

    # UF e município
    municipio_obj = item.get("municipio") or {}
    uf_obj        = municipio_obj.get("uf") or {}
    uf            = uf_obj.get("sigla", "") if isinstance(uf_obj, dict) else str(uf_obj)
    municipio     = municipio_obj.get("nomeIBGE", "") or municipio_obj.get("nome", "")

    # Objeto — está dentro do subobjeto licitacao
    licitacao_obj = item.get("licitacao") or {}
    objeto = licitacao_obj.get("objeto") or item.get("objeto") or item.get("descricao") or ""

    # Modalidade — campo direto
    modalidade = item.get("modalidadeLicitacao") or "Não informada"

    # Valor
    valor = item.get("valor") or item.get("valorEstimado") or item.get("valorTotal")

    # Datas já em formato ISO (YYYY-MM-DD) ou com hora — pegar só os 10 primeiros chars
    data_pub = (item.get("dataPublicacao") or "")[:10]
    prazo    = (item.get("dataAbertura") or item.get("dataEncerramentoProposta") or "")[:10]

    # URL direta no Portal da Transparência
    item_id   = item.get("id") or licitacao_obj.get("numero") or ""
    fonte_url = (
        f"https://portaldatransparencia.gov.br/licitacoes/{item_id}"
        if item_id else "https://portaldatransparencia.gov.br/licitacoes"
    )

    return {
        "_source": "transparencia",
        "orgaoEntidade": {
            "razaoSocial":   orgao_nome,
            "esferaNome":    "Federal",   # Transparência cobre apenas federal
            "ufSigla":       uf,
            "municipioNome": municipio,
        },
        "unidadeOrgao": {
            "ufSigla":       uf,
            "municipioNome": municipio,
        },
        "objetoCompra":            objeto,
        "modalidadeNome":          modalidade,
        "valorTotalEstimado":      float(valor) if valor else None,
        "dataPublicacaoPncp":      data_pub,
        "dataEncerramentoProposta": prazo or None,
        "linkSistemaOrigem":       fonte_url,
    }


def fetch_transparencia(data_ini: date, data_fim: date) -> list:
    """
    Busca licitações no Portal da Transparência para o intervalo de datas.
    Respeita o rate limit configurado (TRANSPARENCIA_RPM = 120 req/min).
    Retorna lista de itens já normalizados para o formato interno.
    """
    api_key = os.environ.get("TRANSPARENCIA_API_KEY", "")
    if not api_key:
        print("   TRANSPARENCIA_API_KEY não configurada — fonte ignorada.")
        return []

    headers = {
        "chave-api-dados": api_key,
        "Accept": "application/json",
    }

    all_items: list = []
    pagina = 1
    total_pages = 1

    print(f"   Buscando Portal da Transparência ({transparencia_date(data_ini)} → {transparencia_date(data_fim)})...")

    while pagina <= total_pages:
        transp_limiter.wait()

        params = {
            "dataInicial": transparencia_date(data_ini),
            "dataFinal":   transparencia_date(data_fim),
            "pagina":      pagina,
            "tamanhoPagina": TRANSPARENCIA_PAGE_SIZE,
        }

        try:
            resp = requests.get(TRANSPARENCIA_BASE, params=params, headers=headers, timeout=30)

            if resp.status_code == 400:
                print("   API da Transparência retornou 400 — parâmetros inválidos.")
                print(f"   Detalhe: {resp.text[:200]}")
                break
            if resp.status_code == 401:
                print("   Chave da API da Transparência inválida ou expirada.")
                break
            if resp.status_code == 429:
                print("   Rate limit atingido na Transparência — aguardando 60s...")
                time.sleep(60)
                continue  # tenta a mesma página novamente
            if resp.status_code == 404:
                break
            resp.raise_for_status()

            data = resp.json()

        except Exception as e:
            print(f"   Erro Transparência pág {pagina}: {e}")
            break

        # A API retorna lista direta (array de LicitacaoDTO)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # fallback: alguns wrappers de API encapsulam em objeto
            items = data.get("data", data.get("content", data.get("items", [])))
            total_reg = data.get("totalRegistros", data.get("total", 0))
            if total_reg and TRANSPARENCIA_PAGE_SIZE:
                total_pages = math.ceil(total_reg / TRANSPARENCIA_PAGE_SIZE)
        else:
            break

        if not items:
            break

        all_items.extend(items)
        print(f"   Transparência pág {pagina}/{total_pages}: {len(items)} itens")

        if len(items) < TRANSPARENCIA_PAGE_SIZE:
            break
        pagina += 1

    normalized = [normalize_transparencia_item(i) for i in all_items]
    print(f"   Total Transparência: {len(normalized)} licitações brutas")
    return normalized


# ---------------------------------------------------------------------------
# DOU — Diário Oficial da União, Seção 3
# ---------------------------------------------------------------------------

def dou_date(d: date) -> str:
    """Converte date para 'DD-MM-YYYY' exigido pela URL do leiturajornal."""
    return d.strftime("%d-%m-%Y")


def _extract_valor_dou(text: str):
    """Tenta extrair valor monetário do texto do DOU. Retorna float ou None."""
    if not text:
        return None
    # Padrões: R$ 1.500.000,00 | R$1500000.00 | 1.500.000,00 (R$) | valor global de R$ ...
    patterns = [
        r"R\$\s*([\d\.]+,\d{2})",
        r"valor[^\d]{0,30}([\d\.]+,\d{2})",
        r"([\d\.]+,\d{2})\s*\(R\$",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(".", "").replace(",", ".")
            try:
                v = float(raw)
                if v >= MIN_VALOR:
                    return v
            except ValueError:
                pass
    return None


def normalize_dou_item(item: dict) -> dict:
    """
    Converte um item do DOU (leiturajornal JSON) para o formato interno.

    Campos relevantes do JSON embutido:
      item.pubName       → nome da publicação (ex: "DO3")
      item.urlTitle      → slug para montar URL do artigo
      item.titulo        → título/cabeçalho (geralmente nome do órgão)
      item.title         → título alternativo
      item.pubDate       → data de publicação (YYYY-MM-DD ou DD/MM/YYYY)
      item.content       → corpo do artigo (texto do edital/aviso)
      item.artType       → tipo de artigo (ex: "Aviso de Licitação")
      item.hierarchyStr  → hierarquia "Ministério > Secretaria > ..."
      item.hierarchyList → lista de níveis hierárquicos
      item.editionNumber → número da edição
    """
    titulo       = item.get("titulo", "") or ""
    title        = item.get("title", "") or ""
    content      = item.get("content", "") or ""
    art_type     = item.get("artType", "") or ""
    hierarchy    = item.get("hierarchyStr", "") or ""
    url_title    = item.get("urlTitle", "") or ""
    pub_date_raw = item.get("pubDate", "") or ""

    # Órgão: hierarquia → preferencialmente 2º nível (mais específico que o ministério,
    # mais reconhecível que a sub-unidade). Ex: "Min. Fazenda/Caixa Econômica Federal/..." → "Caixa Econômica Federal"
    hierarchy_parts = [p.strip() for p in re.split(r"[/>\|]", hierarchy) if p.strip()]
    if len(hierarchy_parts) >= 2:
        orgao = hierarchy_parts[1]          # 2º nível (ex: Caixa Econômica Federal)
    elif hierarchy_parts:
        orgao = hierarchy_parts[0]          # Apenas 1 nível (ex: Prefeituras)
    else:
        orgao = titulo.split("\n")[0].strip() or "DOU"

    # Objeto: combina título + início do conteúdo
    # Remove tags HTML do content
    content_clean = re.sub(r"<[^>]+>", " ", content).strip()
    content_clean = re.sub(r"\s+", " ", content_clean)
    objeto = f"{titulo} — {content_clean[:300]}" if titulo else content_clean[:300]

    # Data: normaliza para ISO
    if pub_date_raw:
        if "/" in pub_date_raw[:5]:
            parts = pub_date_raw[:10].split("/")
            if len(parts) == 3:
                pub_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
            else:
                pub_date = pub_date_raw[:10]
        else:
            pub_date = pub_date_raw[:10]
    else:
        pub_date = date.today().isoformat()

    # Valor estimado (tenta extrair do texto)
    valor = _extract_valor_dou(content_clean)

    # -----------------------------------------------------------------------
    # Modalidade: extrai do texto do artigo (mais específico que o artType)
    # -----------------------------------------------------------------------
    _modalidade_map = [
        (r"PREG[AÃ]O ELETR[OÔ]NICO",        "Pregão Eletrônico"),
        (r"PREG[AÃ]O PRESENCIAL",            "Pregão Presencial"),
        (r"CONCORR[EÊ]NCIA ELETR[OÔ]NICA",  "Concorrência Eletrônica"),
        (r"CONCORR[EÊ]NCIA",                 "Concorrência"),
        (r"DISPENSA ELETR[OÔ]NICA",          "Dispensa Eletrônica"),
        (r"DISPENSA DE LICITA[CÇ][AÃ]O",     "Dispensa de Licitação"),
        (r"INEXIGIBILIDADE",                  "Inexigibilidade"),
        (r"CHAMAMENTO P[UÚ]BLICO",           "Chamamento Público"),
        (r"CREDENCIAMENTO",                  "Credenciamento"),
    ]
    modalidade = art_type if art_type else "Aviso DOU"
    for pat, nome in _modalidade_map:
        if re.search(pat, content_clean, re.IGNORECASE):
            modalidade = nome
            break

    # -----------------------------------------------------------------------
    # Prazo de recebimento de propostas (extraído do texto do artigo)
    # -----------------------------------------------------------------------
    _prazo_patterns = [
        r"Recebimento[^:]*?:\s*(\d{2}/\d{2}/\d{4})",
        r"Abertura das Propostas[^:]*?:\s*(?:dia\s*)?(\d{2}/\d{2}/\d{4})",
        r"Sess[aã]o[^:]*?:\s*(\d{2}/\d{2}/\d{4})",
        r"DATA DA SESS[AÃ]O[^:]*?:\s*(\d{2}/\d{2}/\d{4})",
        r"Entrega das Propostas[^:]*?:\s*(?:a partir de\s*)?(\d{2}/\d{2}/\d{4})",
        r"(\d{2}/\d{2}/\d{4}).*?[àas]\s*\d{1,2}h",   # fallback: data seguida de hora
    ]
    prazo_dou = None
    for pat in _prazo_patterns:
        m = re.search(pat, content_clean, re.IGNORECASE)
        if m:
            prazo_dou = _iso_from_br(m.group(1))
            break

    # -----------------------------------------------------------------------
    # Link do edital: PNCP ou portal gov.br mencionado no texto
    # -----------------------------------------------------------------------
    _edital_link_patterns = [
        r"https?://pncp\.gov\.br/\S+",
        r"https?://www\.gov\.br/compras\S*",
        r"https?://comprasnet\.gov\.br/\S+",
    ]
    link_edital = None
    for pat in _edital_link_patterns:
        m = re.search(pat, content_clean, re.IGNORECASE)
        if m:
            link_edital = m.group(0).rstrip(".,;)")
            break

    # URL do artigo no DOU (link primário — acesso ao aviso oficial)
    if url_title:
        fonte_url = f"https://www.in.gov.br/en/web/dou/-/{url_title}"
    else:
        fonte_url = f"https://www.in.gov.br/leiturajornal?data={dou_date(date.today())}&secao=do3"

    # Âmbito: DOU Seção 3 = federal (esfera do artigo publicador)
    esfera = "Federal"
    uf_match = re.search(r"\b([A-Z]{2})\b", hierarchy)
    uf = uf_match.group(1) if uf_match else ""

    return {
        "_source": "dou",
        "orgaoEntidade": {
            "razaoSocial": orgao,
            "esferaNome":  esfera,
            "ufSigla":     uf,
            "municipioNome": "",
        },
        "unidadeOrgao": {
            "ufSigla":      uf,
            "municipioNome": "",
        },
        "objetoCompra":             objeto,
        "modalidadeNome":           modalidade,
        "valorTotalEstimado":       valor,
        "dataPublicacaoPncp":       pub_date,
        "dataEncerramentoProposta": prazo_dou,
        "linkSistemaOrigem":        fonte_url,
        "_link_edital":             link_edital,   # URL direta para o edital (PNCP/compras)
        "_dou_art_type":            art_type,
        "_dou_content_full":        content_clean[:2000],
    }


def _is_relevant_dou_type(art_type: str) -> bool:
    """
    Retorna True se o artType do DOU indica uma NOVA licitação/edital.
    Exclui explicitamente suspensões, anulações, extratos e ARPs já assinadas.
    """
    norm = normalize_text(art_type)
    # Bloqueia artTypes que não são novas oportunidades
    if norm in {normalize_text(t) for t in DOU_ART_TYPES_EXCLUDE}:
        return False
    # Verifica contra lista de tipos relevantes
    if norm in {normalize_text(t) for t in DOU_ART_TYPES}:
        return True
    # Cobre variantes como "Aviso de Licitação-Pregão", "Aviso de Licitação-Concorrência"
    licitacao_terms = [
        "licitacao", "pregao", "concorrencia", "dispensa",
        "inexigibilidade", "chamamento", "credenciamento",
    ]
    return any(t in norm for t in licitacao_terms)


def _parse_dou_json_from_html(html: str) -> list:
    """
    Extrai os objetos JSON da página leiturajornal do DOU.

    A página embute os dados em uma tag <script> (sem atributo type) como
    um objeto JSON com a chave "jsonArray":
      {"typeNormDay":{...},"section":"DO3","jsonArray":[{...},...]}

    Isso cobre > 99% dos casos. Fallbacks alternativos caso o layout mude.
    """
    soup = BeautifulSoup(html, "lxml")

    # Estratégia principal: script tag contendo "jsonArray":[ e "section":"DO3"
    for tag in soup.find_all("script"):
        text = tag.string or ""
        if '"jsonArray":[' in text and '"section":"DO' in text:
            try:
                data = json.loads(text.strip())
                items = data.get("jsonArray", [])
                if items:
                    print(f"   DOU: {len(items)} itens extraídos do jsonArray")
                    return items
            except json.JSONDecodeError:
                pass

    # Fallback: procura o padrão jsonArray diretamente via regex
    m = re.search(r'"jsonArray"\s*:\s*(\[.*?\])\s*[,}]', html, re.DOTALL)
    if m:
        try:
            items = json.loads(m.group(1))
            print(f"   DOU: {len(items)} itens via regex fallback")
            return items
        except json.JSONDecodeError:
            pass

    print("   DOU: não foi possível extrair JSON embutido da página.")
    return []


def _make_dou_session() -> requests.Session:
    """Cria sessão HTTP com cookies do portal in.gov.br (necessário para leiturajornal)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        session.get("https://www.in.gov.br/", timeout=30)
    except Exception:
        pass  # Continua mesmo sem estabelecer sessão inicial
    return session


def fetch_dou(data_ini: date, data_fim: date) -> list:
    """
    Busca licitações no DOU Seção 3 para o intervalo de datas.
    Usa leiturajornal (não requer autenticação).
    Retorna lista de itens já normalizados para o formato interno.
    """
    session = _make_dou_session()
    all_normalized: list = []
    current = data_ini

    while current <= data_fim:
        date_str = dou_date(current)
        url = f"{DOU_BASE}?data={date_str}&secao=do3"
        print(f"   DOU: buscando {date_str}...")

        try:
            resp = session.get(url, timeout=60)
            if resp.status_code == 404:
                print(f"   DOU: {date_str} sem edição (fim de semana/feriado)")
                current += timedelta(days=1)
                continue
            if resp.status_code != 200:
                print(f"   DOU: HTTP {resp.status_code} para {date_str}")
                current += timedelta(days=1)
                continue
        except Exception as e:
            print(f"   DOU: erro ao buscar {date_str}: {e}")
            current += timedelta(days=1)
            continue

        html = resp.text
        items = _parse_dou_json_from_html(html)

        if not items:
            print(f"   DOU: {date_str} — {len(html)//1024}KB carregados, JSON não extraído")
            current += timedelta(days=1)
            continue

        # Filtra apenas artTypes relacionados a licitação + keyword match
        relevant = []
        for it in items:
            art_type = it.get("artType", "") or ""
            if not _is_relevant_dou_type(art_type):
                continue
            texto = " ".join([
                it.get("titulo", "") or "",
                it.get("title", "") or "",
                it.get("content", "") or "",
                it.get("hierarchyStr", "") or "",
            ])
            # Bloqueia artigos que pelo conteúdo não são novas oportunidades
            # (extratos, suspensões, resultados — independente do artType genérico "aviso")
            titulo_norm = normalize_text(it.get("titulo", "") or "")
            if titulo_norm.startswith(
                tuple(normalize_text(p) for p in DOU_CONTENT_EXCLUDE_PREFIXES)
            ):
                continue
            if keyword_match(texto):
                relevant.append(it)

        # Para log: contar só itens de licitação (sem filtro keyword)
        licit_count = sum(1 for it in items if _is_relevant_dou_type(it.get("artType", "")))
        print(f"   DOU: {date_str} — {licit_count} avisos de licitação; "
              f"{len(relevant)} relevantes por keyword")

        normalized = [normalize_dou_item(i) for i in relevant]
        all_normalized.extend(normalized)

        if current < data_fim:
            time.sleep(2)
        current += timedelta(days=1)

    print(f"   Total DOU: {len(all_normalized)} licitações brutas")
    return all_normalized


# ---------------------------------------------------------------------------
# Querido Diário — Diários Oficiais Municipais
# ---------------------------------------------------------------------------

def normalize_querido_diario_item(gazette: dict) -> dict:
    """
    Converte um gazette do Querido Diário para o formato interno.

    Campos da API:
      gazette.territory_id    → ID IBGE do município
      gazette.territory_name  → nome do município
      gazette.state_code      → sigla do estado (ex: "SP")
      gazette.date            → data de publicação (YYYY-MM-DD)
      gazette.url             → URL canônica no QD
      gazette.excerpts        → lista de trechos extraídos pelo Elasticsearch
    """
    territory_name = gazette.get("territory_name", "") or ""
    state_code     = gazette.get("state_code", "") or ""
    pub_date       = (gazette.get("date", "") or date.today().isoformat())[:10]
    fonte_url      = gazette.get("url", "") or ""
    territory_id   = gazette.get("territory_id", "") or ""
    excerpts       = gazette.get("excerpts", []) or []

    # Objeto: concatena trechos separados por " [...] "
    objeto = " [...] ".join(e.strip() for e in excerpts if e and e.strip())
    if not objeto:
        objeto = f"Diário Oficial de {territory_name} — sem trecho extraído"

    # Órgão: QD não identifica o órgão emitente, usa nome do município
    orgao_nome = territory_name or "Município não informado"

    # Valor: extrai do texto dos trechos
    valor = _extract_valor_dou(objeto)

    # Modalidade: tenta extrair dos trechos via regex
    modalidade = "Aviso de Licitação"
    modalidade_patterns = [
        (r"pregão eletrônico|pregão eletronico|pregao eletronico", "Pregão Eletrônico"),
        (r"pregão presencial|pregao presencial",                   "Pregão Presencial"),
        (r"concorrência|concorrencia",                             "Concorrência"),
        (r"dispensa de licitaç|dispensa de licitac",               "Dispensa de Licitação"),
        (r"inexigibilidade",                                       "Inexigibilidade"),
        (r"chamamento público|chamamento publico",                 "Chamamento Público"),
        (r"credenciamento",                                        "Credenciamento"),
    ]
    objeto_lower = objeto.lower()
    for pattern, nome in modalidade_patterns:
        if re.search(pattern, objeto_lower):
            modalidade = nome
            break

    # Fonte fallback quando url está vazia (evita colisão de MD5)
    if not fonte_url and territory_id and pub_date:
        fonte_url = f"https://queridodiario.ok.org.br/{territory_id}/{pub_date}"

    return {
        "_source": "qd",
        "orgaoEntidade": {
            "razaoSocial":   orgao_nome,
            "esferaNome":    "Municipal",
            "ufSigla":       state_code,
            "municipioNome": territory_name,
        },
        "unidadeOrgao": {
            "ufSigla":       state_code,
            "municipioNome": territory_name,
        },
        "objetoCompra":             objeto,
        "modalidadeNome":           modalidade,
        "valorTotalEstimado":       valor,
        "dataPublicacaoPncp":       pub_date,
        "dataEncerramentoProposta": None,
        "linkSistemaOrigem":        fonte_url,
        "_qd_territory_id":         territory_id,
    }


def fetch_querido_diario(data_ini: date, data_fim: date) -> list:
    """
    Busca licitações nos diários oficiais municipais via API do Querido Diário.
    Executa 3 clusters de query para cobrir os KEYWORDS sem URL excessivamente longa.
    Retorna lista de itens normalizados para o formato interno.
    """
    seen_urls:   set  = set()
    all_gazettes: list = []

    print(f"   Buscando Querido Diário ({data_ini} → {data_fim})...")

    for cluster_idx, query in enumerate(QD_QUERY_CLUSTERS, 1):
        print(f"   QD cluster {cluster_idx}/{len(QD_QUERY_CLUSTERS)}: "
              f"{query[:70].rstrip()}...")
        offset        = 0
        cluster_total = None

        while True:
            params = {
                "querystring":        query,
                "published_since":    data_ini.isoformat(),
                "published_until":    data_fim.isoformat(),
                "size":               QD_PAGE_SIZE,
                "offset":             offset,
                "sort_by":            "relevance",
                "excerpt_size":       QD_EXCERPT_SIZE,
                "number_of_excerpts": QD_NUMBER_OF_EXCERPTS,
            }

            try:
                resp = requests.get(QD_BASE, params=params, timeout=30)

                if resp.status_code == 429:
                    print("   QD: rate limit — aguardando 60s...")
                    time.sleep(60)
                    continue
                if resp.status_code in (502, 503):
                    print(f"   QD: serviço indisponível (HTTP {resp.status_code})")
                    break
                if resp.status_code == 400:
                    print(f"   QD: query inválida (HTTP 400): {resp.text[:200]}")
                    break
                resp.raise_for_status()
                payload = resp.json()

            except Exception as e:
                print(f"   QD: erro cluster {cluster_idx} offset {offset}: {e}")
                break

            gazettes = payload.get("gazettes", [])

            if cluster_total is None:
                cluster_total = payload.get("total_gazettes", 0)
                print(f"   QD cluster {cluster_idx}: {cluster_total} gazettes encontradas")

            if not gazettes:
                break

            for gazette in gazettes:
                url = gazette.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_gazettes.append(gazette)

            offset += QD_PAGE_SIZE
            fetched_so_far = offset
            cap = min(cluster_total or 0, QD_MAX_RESULTS_PER_CLUSTER)
            if fetched_so_far >= cap or len(gazettes) < QD_PAGE_SIZE:
                break

            time.sleep(1)  # rate limit gentil entre páginas

        if cluster_idx < len(QD_QUERY_CLUSTERS):
            time.sleep(2)  # pausa entre clusters

    print(f"   QD: {len(all_gazettes)} gazettes únicas coletadas")

    # Normaliza e aplica keyword_match (excerto pode conter o termo fora de contexto)
    normalized = []
    for g in all_gazettes:
        item = normalize_querido_diario_item(g)
        if keyword_match(item.get("objetoCompra", "")):
            normalized.append(item)

    print(f"   Total QD: {len(normalized)} licitações brutas após keyword match")
    return normalized


# ---------------------------------------------------------------------------
# DODF — Diário Oficial do Distrito Federal
# ---------------------------------------------------------------------------

def date_to_dodf_ts(d: date) -> int:
    """Converte date para o timestamp Unix (meia-noite BRT) usado pelo portal DODF."""
    from datetime import datetime, timezone, timedelta
    dt = datetime(d.year, d.month, d.day, 0, 0, 0,
                  tzinfo=timezone(timedelta(hours=-3)))
    return int(dt.timestamp())


def _make_dodf_session() -> requests.Session:
    """Cria sessão HTTP com cookies do portal dodf.df.gov.br."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://dodf.df.gov.br/",
    })
    try:
        session.get("https://dodf.df.gov.br", timeout=30)
    except Exception:
        pass
    return session


def _fetch_dodf_page(session: requests.Session, ts: int, pagina: int) -> tuple:
    """
    Busca uma página do jornal DODF Seção III.
    Retorna (lista de matérias, total_paginas).
    """
    try:
        resp = session.get(
            DODF_BASE,
            params={"data": ts, "tpSecao": "III", "pagina": pagina},
            timeout=30,
        )
        if resp.status_code not in (200, 202):
            return [], 0
    except Exception as e:
        print(f"   DODF: erro HTTP pág {pagina}: {e}")
        return [], 0

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup.find_all("script"):
        t = tag.string or ""
        if "materiasDiario" not in t:
            continue
        # totalPaginas
        tp_m = re.search(r"var totalPaginas\s*=\s*(\d+)", t)
        total_pags = int(tp_m.group(1)) if tp_m else 1
        # materiasDiario
        md_m = re.search(r"var materiasDiario\s*=\s*(\[.*?\]);\s*var lista", t, re.DOTALL)
        if md_m:
            try:
                return json.loads(md_m.group(1)), total_pags
            except json.JSONDecodeError:
                pass
    return [], 0


def _is_relevant_dodf_tipo(tipo: str) -> bool:
    """Retorna True se o tipo de matéria DODF pode indicar licitação."""
    norm = normalize_text(tipo)
    if norm in {normalize_text(t) for t in DODF_TIPOS_RELEVANTES}:
        return True
    licitacao_terms = [
        "licitacao", "pregao", "concorrencia", "dispensa",
        "inexigibilidade", "chamamento", "credenciamento",
        "abertura", "edital",
    ]
    return any(t in norm for t in licitacao_terms)


def normalize_dodf_item(item: dict, pub_date: str) -> dict:
    """
    Converte uma matéria do DODF para o formato interno.

    Campos relevantes:
      item.coDemandante  → código do demandante
      item.secao         → seção (ex: "Seção III")
      item.poder         → lista de órgãos hierárquicos (ex: ["SETUR", "COMISSÃO..."])
      item.tipo          → tipo da matéria (ex: "Aviso", "Edital")
      item.coMateria     → ID único da matéria
      item.titulo        → título
      item.texto         → texto completo
      item.slug          → slug para URL canônica
    """
    poder        = item.get("poder") or []
    titulo       = item.get("titulo", "") or ""
    texto        = item.get("texto", "") or ""
    tipo         = item.get("tipo", "") or ""
    co_materia   = item.get("coMateria", "") or ""

    # Órgão: primeiro nível do poder (mais específico sem a sub-unidade burocrática)
    # Ex: ['SETUR', 'COMISSÃO ESPECIAL DE LICITAÇÃO'] → 'SETUR'
    orgao_nome = poder[0] if poder else "Órgão DF não informado"

    # Objeto: título + texto (truncado para análise)
    objeto = f"{titulo} — {texto[:400]}" if titulo else texto[:400]

    # Valor: tenta extrair do texto
    valor = _extract_valor_dou(texto)

    # Modalidade: do tipo + título
    modalidade = tipo if tipo else "Aviso DODF"
    modalidade_patterns = [
        (r"pregão eletrônico|pregão eletronico",   "Pregão Eletrônico"),
        (r"pregão presencial|pregao presencial",   "Pregão Presencial"),
        (r"concorrência|concorrencia",             "Concorrência"),
        (r"dispensa",                              "Dispensa de Licitação"),
        (r"inexigibilidade",                       "Inexigibilidade"),
        (r"chamamento",                            "Chamamento Público"),
    ]
    texto_lower = (titulo + " " + texto).lower()
    for pattern, nome in modalidade_patterns:
        if re.search(pattern, texto_lower):
            modalidade = nome
            break

    # URL canônica da matéria
    fonte_url = (
        f"{DODF_MATERIA_URL}?coMateria={co_materia}"
        if co_materia
        else "https://dodf.df.gov.br"
    )

    # Prazo: tenta extrair do texto (datas no formato dd/mm/yyyy após "abertura", "sessão", "prazo")
    prazo = None
    prazo_m = re.search(
        r"(?:abertura|sessão|prazo|encerramento)[^.]{0,60}?(\d{2}/\d{2}/\d{4})",
        texto, re.IGNORECASE
    )
    if prazo_m:
        parts = prazo_m.group(1).split("/")
        try:
            prazo = f"{parts[2]}-{parts[1]}-{parts[0]}"
        except Exception:
            pass

    return {
        "_source": "dodf",
        "orgaoEntidade": {
            "razaoSocial":   orgao_nome,
            "esferaNome":    "Estadual",    # DODF = Governo do DF (estadual)
            "ufSigla":       "DF",
            "municipioNome": "Brasília",
        },
        "unidadeOrgao": {
            "ufSigla":       "DF",
            "municipioNome": "Brasília",
        },
        "objetoCompra":             objeto,
        "modalidadeNome":           modalidade,
        "valorTotalEstimado":       valor,
        "dataPublicacaoPncp":       pub_date,
        "dataEncerramentoProposta": prazo,
        "linkSistemaOrigem":        fonte_url,
        "_dodf_co_materia":         co_materia,
        "_dodf_poder":              poder,
    }


def fetch_dodf(data_ini: date, data_fim: date) -> list:
    """
    Busca licitações no DODF Seção III para o intervalo de datas.
    Usa o endpoint jornal/diario com paginação (10 itens/pág).
    Retorna lista de itens normalizados para o formato interno.
    """
    session      = _make_dodf_session()
    all_normalized: list = []
    seen_materia_ids: set = set()
    current      = data_ini

    while current <= data_fim:
        ts          = date_to_dodf_ts(current)
        pub_date    = current.isoformat()
        pagina      = 1
        total_pages = None

        print(f"   DODF: buscando {current}...")

        while True:
            materias, tp = _fetch_dodf_page(session, ts, pagina)

            if total_pages is None:
                total_pages = tp
                if total_pages == 0:
                    # Sem edição (fim de semana / feriado)
                    print(f"   DODF: {current} sem edição")
                    break
                print(f"   DODF: {current} — {total_pages} páginas, ~{total_pages * DODF_ITEMS_PER_PAGE} matérias")

            if not materias:
                break

            for item in materias:
                co = item.get("coMateria", "")
                if co and co in seen_materia_ids:
                    continue
                if co:
                    seen_materia_ids.add(co)

                # Filtra tipos relevantes para licitação
                if not _is_relevant_dodf_tipo(item.get("tipo", "")):
                    continue

                # Keyword match no título + texto
                texto_full = (item.get("titulo", "") + " " +
                              item.get("texto", "") + " " +
                              " ".join(item.get("poder", [])))
                if not keyword_match(texto_full):
                    continue

                normalized = normalize_dodf_item(item, pub_date)
                all_normalized.append(normalized)

            if pagina >= min(total_pages, DODF_MAX_PAGES):
                break

            pagina  += 1
            time.sleep(0.5)  # gentil com o servidor

        if current < data_fim:
            time.sleep(1)
        current += timedelta(days=1)

    print(f"   Total DODF: {len(all_normalized)} licitações brutas")
    return all_normalized


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def analyze_with_gemini(item: dict, retries=3) -> dict:
    """Analisa licitação com Gemini e retorna JSON estruturado."""
    orgao     = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão não informado")
    objeto    = item.get("objetoCompra", "Objeto não informado")
    modalidade = item.get("modalidadeNome", "Não informada")
    valor     = format_valor(item.get("valorTotalEstimado"))
    data      = item.get("dataPublicacaoPncp", "")[:10]

    prompt = SYSTEM_PROMPT + "\n\n" + ANALYSIS_PROMPT.format(
        orgao=orgao,
        objeto=objeto[:500],
        modalidade=modalidade,
        valor=valor,
        data=data,
    )

    for attempt in range(retries):
        try:
            response = _gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=1000,
                    temperature=0.3,
                ),
            )
            text = response.text.strip()
            # remove blocos de código markdown
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            # localiza o início do objeto JSON
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
            return json.loads(text)

        except Exception as e:
            err = str(e)
            wait = GEMINI_DELAY
            if "retry_delay" in err or "Please retry in" in err:
                m = re.search(r"retry in (\d+)", err)
                wait = int(m.group(1)) + 2 if m else 60
            if attempt < retries - 1 and ("429" in err or "ResourceExhausted" in err):
                print(f"           → Rate limit Gemini, aguardando {wait}s...")
                time.sleep(wait)
                continue
            raise


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _filter_candidates(raw_items: list, existing_ids: set) -> list:
    """Pré-filtro: keyword + valor mínimo + prazo válido + deduplicação."""
    hoje_iso = date.today().isoformat()
    candidates = []
    for item in raw_items:
        objeto = item.get("objetoCompra", "")
        url    = build_pncp_url(item)
        lid    = licitacao_id(url)

        if lid in existing_ids:
            continue
        if not keyword_match(objeto):
            continue
        valor = item.get("valorTotalEstimado")
        if valor is not None and float(valor) < MIN_VALOR:
            continue
        prazo_raw = item.get("dataEncerramentoProposta") or item.get("dataAberturaProposta")
        if prazo_raw and prazo_raw[:10] < hoje_iso:
            continue
        candidates.append(item)
    return candidates


def run():
    print(f"\n{'='*60}")
    print(f"Licitações de Comunicação — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}\n")

    existing_data = load_existing_data()
    existing_ids  = {l["id"] for l in existing_data.get("licitacoes", [])}
    cutoff_date   = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()

    hoje          = date.today()
    dez_dias_atras = hoje - timedelta(days=10)
    janeiro       = date(hoje.year, 1, 1)

    # ------------------------------------------------------------------
    # 1. PNCP
    # ------------------------------------------------------------------
    print(f"1. PNCP ({dez_dias_atras} → {hoje})...\n")
    pncp_raw = fetch_pncp(pncp_date(dez_dias_atras), pncp_date(hoje))
    print(f"\n   Total bruto PNCP: {len(pncp_raw)}\n")

    candidates = _filter_candidates(pncp_raw, existing_ids)

    # Expande para o ano se poucos resultados
    if len(candidates) < 3:
        print(f"   Poucos candidatos PNCP ({len(candidates)}). Expandindo para {janeiro} → {hoje}...\n")
        pncp_ext = fetch_pncp(pncp_date(janeiro), pncp_date(hoje))
        seen = {build_pncp_url(i) for i in pncp_raw}
        novos = [i for i in pncp_ext if build_pncp_url(i) not in seen]
        print(f"\n   {len(novos)} novos itens da janela expandida\n")
        candidates.extend(_filter_candidates(novos, existing_ids))

    # ------------------------------------------------------------------
    # 2. Portal da Transparência (federal + ComprasNet)
    # ------------------------------------------------------------------
    print(f"2. Portal da Transparência ({dez_dias_atras} → {hoje})...\n")
    transp_raw = fetch_transparencia(dez_dias_atras, hoje)

    # Deduplicar contra PNCP (mesma URL pode aparecer nas duas fontes)
    pncp_ids = {licitacao_id(build_pncp_url(i)) for i in pncp_raw}
    transp_new = [i for i in transp_raw if licitacao_id(build_pncp_url(i)) not in pncp_ids]

    transp_candidates = _filter_candidates(transp_new, existing_ids)
    print(f"   {len(transp_candidates)} candidatos da Transparência após filtro\n")

    # ------------------------------------------------------------------
    # 3. DOU — Diário Oficial da União, Seção 3
    # ------------------------------------------------------------------
    print(f"3. DOU Seção 3 ({dez_dias_atras} → {hoje})...\n")
    dou_raw = fetch_dou(dez_dias_atras, hoje)

    # Deduplicar contra PNCP e Transparência
    known_ids = pncp_ids | {licitacao_id(build_pncp_url(i)) for i in transp_raw}
    dou_new = [i for i in dou_raw if licitacao_id(build_pncp_url(i)) not in known_ids]

    dou_candidates = _filter_candidates(dou_new, existing_ids)
    print(f"   {len(dou_candidates)} candidatos do DOU após filtro\n")

    # ------------------------------------------------------------------
    # 4. Querido Diário — Diários Oficiais Municipais
    # ------------------------------------------------------------------
    print(f"4. Querido Diário ({dez_dias_atras} → {hoje})...\n")
    qd_raw = fetch_querido_diario(dez_dias_atras, hoje)

    # Deduplicar contra fontes anteriores por URL/ID
    known_ids_all = known_ids | {licitacao_id(build_pncp_url(i)) for i in dou_raw}
    qd_new = [i for i in qd_raw
              if licitacao_id(build_pncp_url(i)) not in known_ids_all]

    # Deduplicar por objeto (fuzzy) — captura o mesmo edital vindo de outra fonte
    existing_objetos = {
        normalize_text(c.get("objetoCompra", ""))[:100]
        for c in candidates + transp_candidates + dou_candidates
    }
    qd_new = [i for i in qd_new
              if normalize_text(i.get("objetoCompra", ""))[:100] not in existing_objetos]

    qd_candidates = _filter_candidates(qd_new, existing_ids)
    print(f"   {len(qd_candidates)} candidatos do Querido Diário após filtro\n")

    # ------------------------------------------------------------------
    # 5. DODF — Diário Oficial do Distrito Federal
    # ------------------------------------------------------------------
    print(f"5. DODF ({dez_dias_atras} → {hoje})...\n")
    dodf_raw = fetch_dodf(dez_dias_atras, hoje)

    # Deduplicar contra todas as fontes anteriores
    known_ids_dodf = known_ids_all | {licitacao_id(build_pncp_url(i)) for i in qd_raw}
    dodf_new = [i for i in dodf_raw
                if licitacao_id(build_pncp_url(i)) not in known_ids_dodf]

    dodf_candidates = _filter_candidates(dodf_new, existing_ids)
    print(f"   {len(dodf_candidates)} candidatos do DODF após filtro\n")

    all_candidates = candidates + transp_candidates + dou_candidates + qd_candidates + dodf_candidates

    # ------------------------------------------------------------------
    # 6. Análise Gemini
    # ------------------------------------------------------------------
    # Quota por fonte: garante que nenhuma fonte monopolize os slots Gemini.
    # Itens mais recentes têm prioridade dentro de cada fonte.
    total_quota = sum(GEMINI_SOURCE_QUOTAS.values())
    if len(all_candidates) > total_quota:
        by_source: dict = defaultdict(list)
        for c in all_candidates:
            src = c.get("_source", "pncp").lower()
            by_source[src].append(c)

        # Dentro de cada fonte, prioriza os mais recentes
        capped: list = []
        for src, quota in GEMINI_SOURCE_QUOTAS.items():
            bucket = by_source.get(src, [])
            bucket.sort(
                key=lambda x: x.get("dataPublicacaoPncp", ""),
                reverse=True,
            )
            capped.extend(bucket[:quota])

        print(
            f"   Quota aplicada: {len(all_candidates)} → {len(capped)} candidatos "
            f"({', '.join(f'{s}:{min(len(by_source.get(s,[])),q)}' for s,q in GEMINI_SOURCE_QUOTAS.items())})\n"
        )
        all_candidates = capped

    print(f"6. {len(all_candidates)} licitações no pré-filtro. Analisando com Gemini...\n")

    if not all_candidates:
        print("Nenhuma licitação nova para analisar. Atualizando timestamp...")
        existing_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_data(existing_data)
        return

    new_licitacoes = []

    for i, item in enumerate(all_candidates):
        fonte    = item.get("_source", "pncp").upper()
        orgao    = item.get("orgaoEntidade", {}).get("razaoSocial", "—")
        obj_raw  = item.get("objetoCompra", "")
        url      = build_pncp_url(item)

        print(f"   [{i+1}/{len(all_candidates)}] [{fonte}] {orgao[:50]}...")
        print(f"              {obj_raw[:65]}...")

        if i > 0:
            time.sleep(GEMINI_DELAY)

        try:
            analysis = analyze_with_gemini(item)

            if not analysis.get("relevante", False):
                print("           → Não relevante\n")
                continue

            score = analysis.get("score_relevancia", 0)
            # QD tende a não ter valor estimado → threshold levemente menor
            min_score = 4 if fonte == "QD" else 5
            if score < min_score:
                print(f"           → Score baixo ({score}/10)\n")
                continue

            prazo_raw = item.get("dataEncerramentoProposta") or item.get("dataAberturaProposta")
            prazo     = prazo_raw[:10] if prazo_raw else None

            licitacao = {
                "id":              licitacao_id(url),
                "orgao":           orgao,
                "ambito":          parse_ambito(item),
                "objeto":          analysis.get("objeto_resumido", obj_raw[:200]),
                "modalidade":      item.get("modalidadeNome", "Não informada"),
                "valor_estimado":  item.get("valorTotalEstimado"),
                "prazo_proposta":  prazo,
                "data_publicacao": item.get("dataPublicacaoPncp", hoje.isoformat())[:10],
                "fonte_url":       url,
                "link_edital":     item.get("_link_edital"),   # URL edital (PNCP/compras) quando disponível
                "fonte":           fonte,
                "relevance_score": score,
                "categoria":       analysis.get("categoria", "Comunicação Institucional"),
                "justificativa":   analysis.get("justificativa", ""),
            }
            new_licitacoes.append(licitacao)
            print(f"           → {licitacao['categoria']} | Score: {score}/10\n")

        except Exception as e:
            print(f"           → Erro: {e}\n")
            continue

    # ------------------------------------------------------------------
    # 7. Merge, poda e save
    # ------------------------------------------------------------------
    kept = [
        l for l in existing_data.get("licitacoes", [])
        if l.get("data_publicacao", "") >= cutoff_date
    ]

    all_licitacoes = new_licitacoes + kept
    all_licitacoes.sort(
        key=lambda l: (l.get("data_publicacao", ""), l.get("relevance_score", 0)),
        reverse=True,
    )

    save_data({
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total":        len(all_licitacoes),
        "licitacoes":   all_licitacoes,
    })

    print(f"\n{'='*60}")
    print(f"Concluído!")
    print(f"  Novas:    {len(new_licitacoes)} licitações")
    print(f"  Total:    {len(all_licitacoes)} no painel")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
