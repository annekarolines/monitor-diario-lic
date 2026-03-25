#!/usr/bin/env python3
"""
Licitações de Comunicação — Coleta diária do PNCP
Fonte: Portal Nacional de Contratações Públicas (pncp.gov.br)
API pública, sem autenticação necessária.

Endpoint: https://pncp.gov.br/pncp-consulta/v1/contratacoes/publicacao
Formato de data: yyyyMMdd (sem traços)
tamanhoPagina máximo: 50
"""

import os
import json
import hashlib
import time
import re
import unicodedata
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.5-flash")

REQUEST_DELAY = 5       # segundos entre chamadas Gemini (free tier: 20 RPM)
MIN_VALOR     = 10_000.0
MAX_AGE_DAYS  = 30      # licitações mais antigas são removidas do painel
PAGE_SIZE     = 50      # máximo permitido pela API PNCP

PNCP_BASE = "https://pncp.gov.br/pncp-consulta/v1/contratacoes/publicacao"
DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATA_FILE = os.path.join(DATA_DIR, "licitacoes.json")

# Modalidades de contratação a consultar
# 4=Concorrência Eletrônica, 5=Concorrência Presencial
# 6=Pregão Eletrônico, 7=Pregão Presencial
# 8=Dispensa, 9=Inexigibilidade, 3=Concurso
MODALIDADES = [4, 5, 6, 7, 8, 9, 3]

# ---------------------------------------------------------------------------
# Palavras-chave para pré-filtro (antes de qualquer chamada ao Gemini)
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
# Helpers
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
    """Remove acentos e converte para minúsculas para comparação."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def keyword_match(objeto):
    """Retorna True se o objeto contém ao menos uma keyword e nenhuma exclusão."""
    norm = normalize_text(objeto)
    if any(normalize_text(kw) in norm for kw in EXCLUDE_KEYWORDS):
        return False
    return any(normalize_text(kw) in norm for kw in KEYWORDS)


def parse_ambito(item):
    """Deriva string de âmbito a partir dos metadados do PNCP."""
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
    # Fallback: tenta derivar pelo CNPJ do órgão (municipal por padrão)
    if municipio and uf:
        return f"Municipal – {municipio}/{uf}"
    return esfera or "Não informado"


def format_valor(valor):
    """Formata valor numérico para string legível."""
    if valor is None:
        return "não informado"
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "não informado"


def build_pncp_url(item):
    """
    Monta a URL direta no PNCP para o edital.
    Prioridade: linkSistemaOrigem → URL construída com cnpj/ano/sequencial.
    Formato PNCP: https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}
    """
    link_origem = (item.get("linkSistemaOrigem") or "").strip()
    if link_origem:
        return link_origem

    cnpj = item.get("orgaoEntidade", {}).get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
    ano  = item.get("anoCompra") or (item.get("dataPublicacaoPncp", "")[:4])
    seq  = item.get("sequencialCompra", "")
    if cnpj and ano and seq:
        return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}"

    return "https://pncp.gov.br/app/editais"


# ---------------------------------------------------------------------------
# PNCP API
# ---------------------------------------------------------------------------

def pncp_date(d: date) -> str:
    """Converte date para formato yyyyMMdd exigido pela API PNCP."""
    return d.strftime("%Y%m%d")


def fetch_pncp_modalidade(data_ini: str, data_fim: str, modalidade: int) -> list:
    """
    Busca licitações de uma modalidade no intervalo de datas (paginado).
    data_ini / data_fim no formato yyyyMMdd.
    """
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
                import math
                total_pages = math.ceil(total_reg / PAGE_SIZE)

        all_items.extend(items)

        if pagina >= total_pages or len(items) < PAGE_SIZE:
            break
        pagina += 1

    return all_items


def fetch_pncp(data_ini: str, data_fim: str) -> list:
    """Busca todas as modalidades no intervalo e retorna lista unificada."""
    all_items = []
    for cod in MODALIDADES:
        items = fetch_pncp_modalidade(data_ini, data_fim, cod)
        print(f"   Modalidade {cod}: {len(items)} licitações")
        all_items.extend(items)
    return all_items


def determine_date_range(existing_ids: set) -> tuple[str, str]:
    """
    Define o intervalo de busca:
    1. Últimos 10 dias (janela padrão diária)
    2. Se nenhum resultado relevante esperado, expande para 1° de janeiro
    """
    hoje = date.today()
    dez_dias_atras = hoje - timedelta(days=10)
    janeiro = date(hoje.year, 1, 1)

    # Sempre começa pela janela de 10 dias
    return pncp_date(dez_dias_atras), pncp_date(hoje)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def analyze_with_gemini(item, retries=3):
    """Analisa licitação com Gemini e retorna JSON estruturado."""
    orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "Órgão não informado")
    objeto = item.get("objetoCompra", "Objeto não informado")
    modalidade = item.get("modalidadeNome", "Não informada")
    valor = format_valor(item.get("valorTotalEstimado"))
    data = item.get("dataPublicacaoPncp", "")[:10]

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
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            if not text.startswith("{"):
                start = text.find("{")
                if start >= 0:
                    text = text[start:]
            return json.loads(text)

        except Exception as e:
            err = str(e)
            wait = REQUEST_DELAY
            if "retry_delay" in err or "Please retry in" in err:
                m = re.search(r"retry in (\d+)", err)
                wait = int(m.group(1)) + 2 if m else 60
            if attempt < retries - 1 and ("429" in err or "ResourceExhausted" in err):
                print(f"           → Rate limit, aguardando {wait}s...")
                time.sleep(wait)
                continue
            raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print(f"\n{'='*60}")
    print(f"Licitações de Comunicação — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*60}\n")

    existing_data = load_existing_data()
    existing_ids = {l["id"] for l in existing_data.get("licitacoes", [])}
    cutoff_date = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()

    hoje = date.today()
    janeiro = date(hoje.year, 1, 1)
    dez_dias_atras = hoje - timedelta(days=10)

    data_ini = pncp_date(dez_dias_atras)
    data_fim = pncp_date(hoje)

    print(f"1. Buscando licitações do PNCP ({dez_dias_atras} → {hoje})...\n")
    raw_items = fetch_pncp(data_ini, data_fim)
    print(f"\n   Total bruto: {len(raw_items)} licitações\n")

    # Pré-filtro inicial
    candidates = _filter_candidates(raw_items, existing_ids)

    # Se poucos resultados, expande para o ano inteiro
    if len(candidates) < 3:
        print(f"   Poucos resultados ({len(candidates)}). Expandindo para {janeiro} → {hoje}...\n")
        data_ini_ext = pncp_date(janeiro)
        raw_ext = fetch_pncp(data_ini_ext, data_fim)
        # Adiciona apenas itens não vistos ainda
        seen_urls = {build_pncp_url(i) for i in raw_items}
        raw_items_ext = [i for i in raw_ext if build_pncp_url(i) not in seen_urls]
        print(f"\n   {len(raw_items_ext)} novos itens da janela expandida\n")
        candidates.extend(_filter_candidates(raw_items_ext, existing_ids))

    print(f"2. {len(candidates)} licitações no pré-filtro. Analisando com Gemini...\n")

    if not candidates:
        print("Nenhuma licitação nova para analisar. Atualizando timestamp...")
        existing_data["last_updated"] = datetime.now(timezone.utc).isoformat()
        save_data(existing_data)
        return

    new_licitacoes = []

    for i, item in enumerate(candidates):
        orgao = item.get("orgaoEntidade", {}).get("razaoSocial", "—")
        objeto_raw = item.get("objetoCompra", "")
        url = build_pncp_url(item)

        print(f"   [{i+1}/{len(candidates)}] {orgao[:55]}...")
        print(f"              {objeto_raw[:65]}...")

        if i > 0:
            time.sleep(REQUEST_DELAY)

        try:
            analysis = analyze_with_gemini(item)

            if not analysis.get("relevante", False):
                print("           → Não relevante, ignorando\n")
                continue

            score = analysis.get("score_relevancia", 0)
            if score < 5:
                print(f"           → Score baixo ({score}/10), ignorando\n")
                continue

            prazo_raw = (item.get("dataEncerramentoProposta") or
                         item.get("dataAberturaProposta"))
            prazo = prazo_raw[:10] if prazo_raw else None

            licitacao = {
                "id": licitacao_id(url),
                "orgao": orgao,
                "ambito": parse_ambito(item),
                "objeto": analysis.get("objeto_resumido", objeto_raw[:200]),
                "modalidade": item.get("modalidadeNome", "Não informada"),
                "valor_estimado": item.get("valorTotalEstimado"),
                "prazo_proposta": prazo,
                "data_publicacao": item.get("dataPublicacaoPncp", hoje.isoformat())[:10],
                "fonte_url": url,
                "relevance_score": score,
                "categoria": analysis.get("categoria", "Comunicação Institucional"),
                "justificativa": analysis.get("justificativa", ""),
            }
            new_licitacoes.append(licitacao)
            print(f"           → {licitacao['categoria']} | Score: {score}/10\n")

        except (json.JSONDecodeError, KeyError, Exception) as e:
            print(f"           → Erro: {e}\n")
            continue

    kept = [
        l for l in existing_data.get("licitacoes", [])
        if l.get("data_publicacao", "") >= cutoff_date
    ]

    all_licitacoes = new_licitacoes + kept
    all_licitacoes.sort(
        key=lambda l: (l.get("data_publicacao", ""), l.get("relevance_score", 0)),
        reverse=True
    )

    result = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total": len(all_licitacoes),
        "licitacoes": all_licitacoes,
    }

    save_data(result)

    print(f"\n{'='*60}")
    print(f"Concluído! {len(new_licitacoes)} licitações novas adicionadas.")
    print(f"Total no painel: {len(all_licitacoes)} licitações")
    print(f"{'='*60}\n")


def _filter_candidates(raw_items: list, existing_ids: set) -> list:
    """Aplica pré-filtro de keyword + valor + deduplicação."""
    candidates = []
    for item in raw_items:
        objeto = item.get("objetoCompra", "")
        url = build_pncp_url(item)
        lid = licitacao_id(url)
        if lid in existing_ids:
            continue
        if not keyword_match(objeto):
            continue
        valor = item.get("valorTotalEstimado")
        if valor is not None and float(valor) < MIN_VALOR:
            continue
        candidates.append(item)
    return candidates


if __name__ == "__main__":
    run()
