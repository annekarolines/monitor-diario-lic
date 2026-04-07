#!/usr/bin/env python3
"""
Licitações de Comunicação — Coleta diária
Fontes:
  1. PNCP  — Portal Nacional de Contratações Públicas (pncp.gov.br)
  2. Portal da Transparência — api.portaldatransparencia.gov.br

Limites Portal da Transparência:
  - 00h-06h BRT: 700 req/min
  - Demais horários: 400 req/min
  - APIs restritas: 180 req/min
  Usamos 120 req/min como margem segura (abaixo do limite restrito).
"""

import os
import json
import hashlib
import time
import re
import math
import unicodedata
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.5-flash")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

GEMINI_DELAY  = 5        # segundos entre chamadas Gemini (free tier: 20 RPM)
MIN_VALOR     = 10_000.0
MAX_AGE_DAYS  = 30
PAGE_SIZE     = 50       # máximo PNCP

# PNCP
PNCP_BASE = "https://pncp.gov.br/pncp-consulta/v1/contratacoes/publicacao"
# Modalidades: 3=Concurso, 4=Concorrência Eletrônica, 5=Concorrência Presencial,
#              6=Pregão Eletrônico, 7=Pregão Presencial, 8=Dispensa, 9=Inexigibilidade
MODALIDADES = [4, 5, 6, 7, 8, 9, 3]

# Portal da Transparência
TRANSPARENCIA_BASE      = "https://api.portaldatransparencia.gov.br/api-de-dados/licitacoes"
TRANSPARENCIA_PAGE_SIZE = 500   # máximo aceito pela API
TRANSPARENCIA_RPM       = 120   # conservador: abaixo do limite de 180 (APIs restritas)

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
    "assessoria de imprensa",
    "assessoria à imprensa",
    "fornecimento de equipamento",
    "equipamentos de informática",
    "equipamentos de informatica",
    "radiocomunicação",
    "radiocomunicacao",
    "sistema de comunicacao de dados",
    "sistema de comunicação de dados",
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
    Converte um item do Portal da Transparência para o formato interno
    compatível com as funções existentes (parse_ambito, build_pncp_url, etc.).
    Múltiplos fallbacks para lidar com variações de schema da API.
    """
    # Órgão
    orgao_nome = (
        item.get("unidadeGestora", {}).get("nome") or
        item.get("unidadeGestora", {}).get("descricao") or
        item.get("orgaoSuperior", {}).get("nome") or
        item.get("orgao", {}).get("nome") or
        "Órgão não informado"
    )

    # UF e município
    uf        = item.get("municipio", {}).get("uf", "")
    municipio = item.get("municipio", {}).get("nome", "")

    # Objeto
    objeto = item.get("objeto") or item.get("descricao") or ""

    # Modalidade
    modalidade = (
        item.get("modalidade", {}).get("descricao") or
        item.get("tipo", {}).get("descricao") or
        "Não informada"
    )

    # Valor
    valor = (
        item.get("valor") or
        item.get("valorEstimado") or
        item.get("valorTotal") or
        item.get("valorContrato")
    )

    # Datas
    data_pub = _iso_from_br(
        item.get("dataPublicacao") or item.get("dataAbertura") or ""
    )
    prazo = _iso_from_br(
        item.get("dataEncerramentoProposta") or
        item.get("dataAberturaProposta") or ""
    )

    # URL direta no Portal da Transparência
    item_id  = item.get("id") or item.get("numero") or ""
    fonte_url = (
        f"https://portaldatransparencia.gov.br/licitacoes/{item_id}"
        if item_id else "https://portaldatransparencia.gov.br/licitacoes"
    )

    return {
        "_source": "transparencia",
        "orgaoEntidade": {
            "razaoSocial":  orgao_nome,
            "esferaNome":   "Federal",   # Transparência cobre apenas federal
            "ufSigla":      uf,
            "municipioNome": municipio,
        },
        "unidadeOrgao": {
            "ufSigla":      uf,
            "municipioNome": municipio,
        },
        "objetoCompra":           objeto,
        "modalidadeNome":         modalidade,
        "valorTotalEstimado":     float(valor) if valor else None,
        "dataPublicacaoPncp":     data_pub,
        "dataEncerramentoProposta": prazo or None,
        "linkSistemaOrigem":      fonte_url,
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

        # A API pode retornar lista direta ou objeto paginado
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
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
            response = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
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

    all_candidates = candidates + transp_candidates

    # ------------------------------------------------------------------
    # 3. Análise Gemini
    # ------------------------------------------------------------------
    print(f"3. {len(all_candidates)} licitações no pré-filtro. Analisando com Gemini...\n")

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
            if score < 5:
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
    # 4. Merge, poda e save
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
