# agent.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Agent — o cérebro do sistema.
#
# Loop agêntico:
#   1. Recebe mensagem do usuário
#   2. LLM decide: responder diretamente OU chamar uma ferramenta
#   3. Se ferramenta → executa → devolve resultado ao LLM → resposta final
#   4. Persiste tudo no histórico de conversa
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime

import database as db
import tools as tool_registry
from llm import call_llm

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM = """
Você é o FinBot, um assistente financeiro pessoal via WhatsApp.
Seu objetivo: ajudar o usuário a registrar gastos, receitas (incomes) e entender sua vida financeira.

PERSONALIDADE:
- Direto, amigável e sem enrolação (é WhatsApp, não email)
- Use emojis com moderação para deixar as mensagens mais legíveis
- Responda sempre em português do Brasil

REGRAS IMPORTANTES:
- Registre gastos SEM pedir confirmação — o usuário quer agilidade
- Sempre informe o total acumulado (da categoria para gastos ou total do mês para receitas) após um registro
- Ao mostrar valores, use formato R$ X.XXX,XX
- Se o usuário escrever algo ambíguo, interprete pelo contexto e aja
- Nunca invente dados financeiros — use apenas os dados das ferramentas
- Identifique o beneficiário sempre que o usuário usar "para Fulano", "pro Fulano" ou mencionar um recebedor.
- Se o usuário pedir algo fora do escopo financeiro, redirecione com gentileza

INTERPRETAÇÃO DE MENSAGENS:
- "almoço 35" → gasto de R$ 35,00 em Alimentação
- "uber 12,50" → gasto de R$ 12,50 em Transporte
- "recebi 5000 salário" → receita de R$ 5.000,00 em Salário
- "receita aluguel 3800" → receita de R$ 3.800,00 em Extra
- "uber 12,50 credito" → gasto de R$ 12,50 em Transporte no crédito
- "tênis 300 3x credito" → crédito parcelado, valor TOTAL R$ 300,00 em 3x de R$ 100,00
- "mercado 180 no cartão" → crédito, R$ 180,00 em Alimentação
- "almoço para João 35" → gasto de R$ 35,00 em Alimentação, beneficiário "João"
- "farmácia 45" → gasto de R$ 45,00 em Saúde
- "almoço 35 03/05" → gasto de R$ 35,00 em Alimentação na data 03/05
- "uber 12,50 03/05 credito" → crédito, R$ 12,50 em Transporte na data 03/05
- "farmacia 2x 50,00 03/05 credito" → crédito parcelado 2x na data 03/05
- "resumo" / "quanto gastei" → chamar resumo_mensal
- "fatura" / "cartão" / "quanto vou pagar" → chamar consultar_fatura
- "últimos gastos" / "histórico" → chamar ultimos_gastos
- "meu cartão vence dia X" → chamar configurar_cartao
- "limite alimentação 2000" / "meta transporte 500" / "teto moradia 3000" → chamar definir_limite
- "meus limites" / "meu orçamento" → chamar consultar_limite
- "qual meu limite de alimentação" → chamar consultar_limite com categoria
- "histórico do limite de transporte" → chamar consultar_limite com historico=true

ORÇAMENTO:
- Ao definir um limite, mostre o percentual já usado no mês atual
- No resumo mensal, se houver limites definidos, mostre o % usado ao lado de cada categoria
- Use 🔴 quando ultrapassou o limite, ⚠️ quando passou de 80%, ✅ quando está ok

DATA MANUAL:
- Compras parceladas: o valor informado pelo usuário é sempre o valor TOTAL da compra.
- Quando o usuário informar uma data (ex: 03/05), use essa data para registrar o gasto
- Para crédito com data manual, a fatura é calculada com base na data informada

CARTÃO DE CRÉDITO:
- Ao confirmar gasto no crédito, sempre mostre em qual fatura vai cair
- Parcelado: mostre o valor de cada parcela e em quais faturas vão cair

FORMATO DAS RESPOSTAS:
- Gasto débito: valor e descrição
- Gasto crédito: valor, descrição e fatura que vai cair (ex: "fatura junho/2026")
- Gasto parcelado: descrição, valor total, valor de cada parcela e em qual fatura cai
- Fatura: total e lista dos principais lançamentos
- Resumo: cada categoria com valor e percentual, depois total geral
- Não precisa exibir categoria na mensagem de confirmação do gasto
- Seja conciso — o usuário está no celular
""".strip()

ONBOARDING_MSG = (
    "👋 Olá! Sou o *FinBot*, seu assistente de controle financeiro.\n\n"
    "Para começar, me diz: *qual o dia de vencimento da sua fatura do cartão de crédito?*\n\n"
    "_(Se não usar cartão, pode digitar qualquer dia — ex: 1)_"
)

def _fmt(value: float) -> str:
    """Formata valor monetário com . para milhar e , para decimal. Ex: 1.234,56"""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _normalize(text: str) -> str:
    """Remove acentos e converte para minúsculas para comparação robusta."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).lower()

# ── Complexity classifier ─────────────────────────────────────────────────────
# Simple messages are handled with regex + direct tool call — zero LLM cost.
# Complex messages go through the full agentic loop.

_TRANSFER_RE = re.compile(
    r"^transfer[êe]ncia\s+(?P<valor>\d+(?:[.,]\d{1,2})?)\s+para\s+(?P<beneficiario>.+)$",
    re.IGNORECASE,
)

_INCOME_RE = re.compile(
    r"^(?P<desc>(?:receita|sal[áa]rio|recebi|ganhei|pix\s+recebido|rendimento|entrada|venda|reembolso|b[ôo]nus|pr[ée]mio)(?:\s+[a-zA-ZÀ-ÿ ]+)?)\s+"
    r"(?P<valor>\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?))?"
    r"(?:\s+(?:de|do|da|proveniente\s+de)\s+(?P<pagador>.+))?\s*$",
    re.IGNORECASE,
)

_INCOME_ALT_RE = re.compile(
    r"^(?P<valor>\d+(?:[.,]\d{1,2})?)\s+"
    r"(?:(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)\s+)?"
    r"(?:(?:de|do|da|em|referente\s+ao?)\s+)?"
    r"(?P<desc>(?:receita|sal[áa]rio|recebi|ganhei|pix\s+recebido|rendimento|entrada|venda|reembolso|b[ôo]nus|pr[ée]mio)(?:\s+[a-zA-ZÀ-ÿ ]+)?)\s*$",
    re.IGNORECASE,
)

# Budget pattern: "limite alimentação 2000", "meta transporte 500", "teto moradia 3000"
_BUDGET_RE = re.compile(
    r"^(?:limite|meta|teto|or[çc]amento|budget)\s+"
    r"(?P<categoria>[a-zA-ZÀ-ÿ]+(?:\s+[a-zA-ZÀ-ÿ]+)?)\s+"
    r"(?P<valor>\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<mes>\d{4}-\d{2}|\d{1,2}[/-]\d{4}))?$",
    re.IGNORECASE,
)

_BUDGET_CAT_MAP = {
    "alimenta": "Alimentação", "comida": "Alimentação", "mercado": "Alimentação",
    "transport": "Transporte", "uber": "Transporte",
    "moradia": "Moradia", "aluguel": "Moradia", "casa": "Moradia",
    "saude": "Saúde", "saúde": "Saúde", "farmacia": "Saúde",
    "lazer": "Lazer", "entretenimento": "Lazer",
    "pessoal": "Pessoal", "beleza": "Pessoal",
    "educacao": "Educação", "educação": "Educação", "curso": "Educação",
    "financeiro": "Financeiro", "seguro": "Financeiro",
}

_EXPENSE_RE = re.compile(
    r"^(?P<desc>[a-zA-ZÀ-ÿ0-9 ]+?)\s+"
    r"(?:(?P<parcelas_pre>\d+)[xX]\s+)?"
    r"(?P<valor>\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<parcelas_pos>\d+)[xX])?"
    r"(?:\s+(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?))?"
    r"(?:\s+(?P<method>cr[ée]d(?:ito)?|cart[ãa]o|d[ée]b(?:ito)?|dinheiro))?"
    r"(?:\s+(?:para|pro|pra)\s+(?P<beneficiario>.+))?$",
    re.IGNORECASE,
)

_KEYWORD_TOOLS: dict[str, str] = {
    "resumo":          "resumo_mensal",
    "relatório":       "resumo_mensal",
    "relatorio":       "resumo_mensal",
    "quanto gastei":   "resumo_mensal",
    "minhas finanças": "resumo_mensal",
    "fatura":          "consultar_fatura",
    "cartão":          "consultar_fatura",
    "cartao":          "consultar_fatura",
    "quanto vou pagar":"consultar_fatura",
    "últimos gastos":  "ultimos_gastos",
    "ultimos gastos":  "ultimos_gastos",
    "historico":       "ultimos_gastos",
    "histórico":       "ultimos_gastos",
    "o que registrei": "ultimos_gastos",
    "lista":           "listar_categoria",
    "detalhe":         "listar_categoria",
    "detalhes":        "listar_categoria",
    "o que comprei":   "listar_categoria",
    "tendência":       "tendencia_semanal",
    "tendencia":       "tendencia_semanal",
    "essa semana":     "tendencia_semanal",
    "meus limites":    "consultar_limite",
    "meu orçamento":   "consultar_limite",
    "meu orcamento":   "consultar_limite",
    "limites":         "consultar_limite",
    "orcamento":       "consultar_limite",
    "orçamento":       "consultar_limite",
    "categorias":      "listar_categorias_disponiveis",
    "quais categorias": "listar_categorias_disponiveis",
    "lista categorias": "listar_categorias_disponiveis",
    "subcategorias":   "listar_categorias_disponiveis",
    "sincronizar":     "sincronizar_banco",
    "banco":           "sincronizar_banco",
    "atualizar":       "sincronizar_banco",
    "novidades":       "sincronizar_banco",
    "ajuda":           "ajuda",
    "help":            "ajuda",
    "como":            "ajuda",
    "onde":            "ajuda",
    "aonde":           "ajuda",
    "instruções":      "ajuda",
    "instrucao":       "ajuda",
}

_CATEGORIES = {
    # Alimentação
    "ifood": "Alimentação",
    "alimentacao": "Alimentação",
    "refeicao": "Alimentação",
    "refeição": "Alimentação",
    "almoco": "Alimentação",
    "almoço": "Alimentação",   
    "janta": "Alimentação",
    "jantar": "Alimentação",
    "lanche": "Alimentação",
    "cafe": "Alimentação",
    "café": "Alimentação",    
    "padaria": "Alimentação",
    "mercado": "Alimentação",
    "supermercado": "Alimentação",
    "pizza": "Alimentação",
    "hamburguer": "Alimentação",
    "hambúrguer": "Alimentação",
    "lanchonete": "Alimentação",
    "cereais": "Alimentação",
    "hortifruti": "Alimentação",
    "hortifrúti": "Alimentação",
    "acougue": "Alimentação",
    "açougue": "Alimentação",
    "peixaria": "Alimentação",
    "restaurante": "Alimentação",

    # Transporte
    "uber": "Transporte",
    "uber moto": "Transporte",
    "99": "Transporte",
    "gasolina": "Transporte",
    "posto": "Transporte",
    "estacionamento": "Transporte",
    "onibus": "Transporte",
    "metro": "Transporte",
    "transporte": "Transporte",
    "taxi": "Transporte",
    "passagem": "Transporte",
    "manutencao": "Transporte",
    "oficina": "Transporte",
    "mecânico": "Transporte",
    "mecanico": "Transporte",
    "revisão": "Transporte",
    "revisao": "Transporte",
    "pneu": "Transporte",

    # Moradia
    "aluguel": "Moradia",
    "condominio": "Moradia",
    "condomínio": "Moradia",    
    "luz": "Moradia",
    "água": "Moradia",
    "agua": "Moradia",
    "internet": "Moradia",
    "telefone": "Moradia",
    "gás": "Moradia",
    "gas": "Moradia",
    "faxina": "Moradia",
    "faxineira": "Moradia",
    "limpeza": "Moradia",
    "casa": "Moradia",
    "utensílios": "Moradia",
    "utensilios": "Moradia",
    "móveis": "Moradia",
    "moveis": "Moradia",
    "decoração": "Moradia",
    "decoracao": "Moradia",    
    "reforma": "Moradia",

    # Saúde
    "farmácia": "Saúde",
    "farmacia": "Saúde",
    "remédio": "Saúde",
    "remedio": "Saúde",
    "médico": "Saúde",
    "medico": "Saúde",
    "academia": "Saúde",
    "musculação": "Saúde",
    "musculacao": "Saúde",
    "suplemento": "Saúde",
    "suplementos": "Saúde",
    "whey": "Saúde",
    "crossfit": "Saúde",
    "yoga": "Saúde",
    "pilates": "Saúde",
    "dentista": "Saúde",
    "consulta": "Saúde",
    "exame": "Saúde",
    "plano de saude": "Saúde",
    "plano de saúde": "Saúde",
    "plano saude": "Saúde",
    "plano saúde": "Saúde",
    "convenio": "Saúde",
    "convênio": "Saúde",

    # Pets
    "pet": "Pets",
    "petshop": "Pets",
    "veterinário": "Pets",
    "veterinario": "Pets",
    "racao": "Pets",
    "ração": "Pets",
    "banho": "Pets",
    "tosa": "Pets",

    # Lazer / Social
    "netflix": "Lazer",
    "spotify": "Lazer",
    "hbo": "Lazer",
    "max": "Lazer",
    "disney": "Lazer",
    "prime video": "Lazer",
    "globoplay": "Lazer",
    "cinema": "Lazer",
    "show": "Lazer",
    "ingresso": "Lazer",
    "viagem": "Lazer",
    "restaurante": "Lazer",
    "bar": "Lazer",
    "social": "Lazer",
    "cerveja": "Lazer",
    "chopp": "Lazer",
    "balada": "Lazer",
    "presente": "Lazer",
    "gift": "Lazer",
    "lembrancinha": "Lazer",
    "mimo": "Lazer",

    # Educação
    "curso": "Educação",
    "livro": "Educação",
    "escola": "Educação",
    "faculdade": "Educação",
    "udemy": "Educação",
    "linkedin": "Educação",
    "software": "Educação",
    "api": "Educação",

    # Vestuário -> Pessoal
    "roupa": "Pessoal",
    "tenis": "Pessoal",
    "sapato": "Pessoal",
    "camiseta": "Pessoal",

    # Beleza e Cuidados -> Pessoal
    "cabelo": "Pessoal",
    "barbeiro": "Pessoal",
    "barba": "Pessoal",
    "salao": "Pessoal",
    "salão": "Pessoal",
    "estetica": "Pessoal",
    "estética": "Pessoal",

    # Financeiro
    "seguro": "Financeiro",
    "tarifa": "Financeiro",
    "anuidade": "Financeiro",
    "banco": "Financeiro",
    "imposto": "Financeiro",
    "taxa": "Financeiro",
}

def _levenshtein(a: str, b: str) -> int:
    """Calcula a distancia de edicao entre duas strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = range(len(b) + 1)
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _infer_category(desc: str) -> str:
    """Infere categoria pela descricao.
    
    1. Busca exata no dicionario _CATEGORIES (comportamento original).
    2. Busca fuzzy por distancia de edicao para cobrir erros de digitacao.
       Tolerancia: 1 caractere para palavras ate 5 letras, 2 para maiores.
    3. Retorna "Outros" se nao encontrar — cai no LLM para classificar.
    """
    desc_norm = _normalize(desc)

    # 1. Busca exata
    for keyword, category in _CATEGORIES.items():
        if keyword in desc_norm:
            return category

    # 2. Busca fuzzy por palavra
    single_keywords = {k: v for k, v in _CATEGORIES.items() if " " not in k}
    for word in desc_norm.split():
        if len(word) < 4:  # ignora palavras muito curtas: "de", "um", "no"
            continue
        best_dist = 999
        best_cat = None
        for keyword, category in single_keywords.items():
            if abs(len(word) - len(keyword)) > 2:  # otimizacao: pula se tamanho muito diferente
                continue
            dist = _levenshtein(word, keyword)
            if dist < best_dist:
                best_dist = dist
                best_cat = category
        max_dist = 1 if len(word) <= 5 else 2
        if best_dist <= max_dist and best_cat:
            return best_cat

    return "Outros"



def _split_descricao_beneficiario(desc: str) -> tuple[str, str | None]:
    text = " ".join(desc.strip().split())
    lower = text.lower()

    if " para " in lower:
        i = lower.rfind(" para ")
        descricao = text[:i].strip()
        beneficiario = text[i + 6:].strip()
        if descricao and beneficiario:
            return descricao, beneficiario

    if lower.startswith("pix "):
        beneficiario = text[4:].strip()
        if beneficiario:
            return "pix", beneficiario

    return text, None

def _parse_method(raw: str | None) -> str:
    if not raw:
        return "debito"
    raw = raw.lower().strip()
    if any(w in raw for w in ["créd", "cred", "cartão", "cartao"]):
        return "credito"
    if "dinheiro" in raw:
        return "dinheiro"
    return "debito"


def _parse_date(raw: str | None) -> date | None:
    """
    Parse date string from user input.
    Accepts: "03/05", "03/05/26", "03/05/2026", "03-05", "03.05"
    Returns a date object or None if invalid.
    """
    if not raw:
        return None
    raw = raw.strip().replace("-", "/").replace(".", "/")
    parts = raw.split("/")
    today = date.today()
    try:
        if len(parts) == 2:
            # DD/MM — assume current year
            d, m = int(parts[0]), int(parts[1])
            return date(today.year, m, d)
        elif len(parts) == 3:
            d, m = int(parts[0]), int(parts[1])
            y = int(parts[2])
            if y < 100:
                y += 2000
            return date(y, m, d)
    except ValueError:
        return None
    return None


def _classify(message: str) -> dict | None:
    """
    Try to classify the message without an LLM.
    Returns a dict with tool + args if simple, or None if complex.
    """
    msg = message.strip()
    msg_norm = _normalize(msg)

    # Keyword-based tool dispatch
    for keyword, tool_name in _KEYWORD_TOOLS.items():
        if _normalize(keyword) in msg_norm:
            if tool_name == "listar_categoria":
                # Tenta extrair categoria da mensagem
                # Ex: "lista alimentacao", "detalhes saude", "o que comprei em lazer"
                _CAT_MAP = {
                    "alimenta": "Alimentação", "comida": "Alimentação", "mercado": "Alimentação",
                    "transport": "Transporte", "uber": "Transporte", "carro": "Transporte",
                    "moradia": "Moradia", "aluguel": "Moradia", "casa": "Moradia",
                    "saude": "Saúde", "farmacia": "Saúde", "medico": "Saúde",
                    "lazer": "Lazer", "restaurante": "Lazer", "cinema": "Lazer",
                    "pessoal": "Pessoal", "roupa": "Pessoal", "cabelo": "Pessoal",
                    "educacao": "Educação", "curso": "Educação", "livro": "Educação",
                    "financeiro": "Financeiro", "seguro": "Financeiro",
                    "pets": "Pets", "pet": "Pets", "veterinario": "Pets",
                }
                categoria = None
                for key, cat in _CAT_MAP.items():
                    if key in msg_norm:
                        categoria = cat
                        break
                # If no specific category is found for "listar_categoria",
                # we let the LLM handle it to infer the category.
                # If we return None here, it will fall through to the LLM.
                if categoria:
                    return {"tool": tool_name, "args": {"categoria": categoria}}
                # Sem categoria identificada → cai no LLM
                return None
            return {"tool": tool_name, "args": {}}

    # ── Budget pattern ────────────────────────────────────────────────────────
    # The order of these checks matters. More specific patterns should come before
    # more general ones to avoid misclassification. For example, a budget command
    # should not be interpreted as a general expense.
    # Must come BEFORE expense pattern to avoid "limite alimentação 2000"
    # being parsed as a R$ 2000 expense called "limite alimentação"
    m_budget = _BUDGET_RE.match(msg)
    if m_budget:
        cat_raw = _normalize(m_budget.group("categoria"))
        categoria = None
        for key, cat in _BUDGET_CAT_MAP.items():
            if _normalize(key) in cat_raw or cat_raw in _normalize(key):
                categoria = cat
                break
        if not categoria:
            return None  # unknown category → let LLM handle

        valor = float(m_budget.group("valor").replace(",", "."))
        args: dict = {"categoria": categoria, "valor": valor}

        mes_raw = m_budget.group("mes")
        if mes_raw:
            args["mes"] = mes_raw

        return {"tool": "definir_limite", "args": args}

    # ── Income pattern ───────────────────────────────────────────────────────
    m_income = _INCOME_RE.match(msg) or _INCOME_ALT_RE.match(msg)
    if m_income:
        groups = m_income.groupdict()
        desc_raw = (groups.get("desc") or "").lower()
        valor = float((groups.get("valor") or "0").replace(",", "."))
        pagador = (groups.get("pagador") or "").strip() or None
        expense_date = _parse_date(groups.get("data"))
        
        cat = "Salário" if "sal" in desc_raw else "Extra"
        if "rend" in desc_raw: cat = "Investimento"
        elif "presente" in desc_raw: cat = "Presente"
        elif "reembolso" in desc_raw: cat = "Reembolso"

        args = {
            "valor": valor,
            "categoria": cat,
            "descricao": desc_raw,
        }
        if pagador: args["pagador"] = pagador
        if expense_date: args["data"] = expense_date.isoformat()

        return {
            "tool": "registrar_receita",
            "args": args
        }

    # Transfer pattern: "transferencia 20 para renate"
    m_transfer = _TRANSFER_RE.match(msg)
    if m_transfer:
        return {
            "tool": "registrar_gasto",
            "args": {
                "valor": float(m_transfer.group("valor").replace(",", ".")),
                "categoria": "Outros",
                "descricao": "transferencia",
                "beneficiario": m_transfer.group("beneficiario").strip(),
                "payment_method": "debito",
            },
        }

    # Expense pattern
    m = _EXPENSE_RE.match(msg)
    if m:
        desc_raw = m.group("desc").strip()

        # Beneficiario from regex group (ex: "almoço 35 para João")
        beneficiario = m.group("beneficiario").strip() if m.group("beneficiario") else None

        # If not captured by suffix, try splitting desc (ex: "almoço para João 35")
        if not beneficiario:
            descricao, beneficiario = _split_descricao_beneficiario(desc_raw)
        else:
            descricao = desc_raw

        valor = float(m.group("valor").replace(",", "."))
        method = _parse_method(m.group("method"))
        parc = int(m.group("parcelas_pre") or m.group("parcelas_pos") or 1)
        cat = _infer_category(descricao)
        expense_date = _parse_date(m.group("data"))

        args: dict = {
            "valor": valor,
            "categoria": cat,
            "descricao": descricao,
            "payment_method": method,
        }

        if expense_date:
            args["data"] = expense_date.isoformat()

        if beneficiario:
            args["beneficiario"] = beneficiario

        if method == "credito" and parc > 1:
            args["parcelas"] = parc

        return {"tool": "registrar_gasto", "args": args}

    return None

async def _fast_path(tool_name: str, args: dict, user_phone: str) -> str:
    """Execute a tool directly and return a formatted reply without calling the LLM."""
    if tool_name == "ajuda":
        return (
            "Olá! Sou o *FinBot*, seu assistente financeiro. 😊\n\n"
            "Posso te ajudar a organizar suas finanças registrando o que entra e o que sai.\n\n"
            "*O que você gostaria de fazer?*\n\n"
            "*   *Registrar um gasto:* Diga o valor e o que comprou (ex: \"almoço 35\" ou \"uber 12,50\").\n"
            "*   *Cartão de Crédito:* Adicione 'crédito' ou 'cartão' (ex: \"ifood 50 crédito\").\n"
            "*   *Parcelamento:* Informe as parcelas (ex: \"tênis 300 3x crédito\").\n"
            "*   *Registrar uma receita:* Diga o que recebeu (ex: \"recebi 5000 salário\" ou \"pix 100 de João\").\n"
            "*   *Ver um resumo:* Diga \"resumo\" para ver suas finanças por categoria.\n"
            "*   *Consultar a fatura:* Peça \"fatura\" ou \"cartão\" para ver o que vai pagar.\n"
            "*   *Configurar o cartão:* Me diga \"meu cartão vence dia X\" para configurar a data de vencimento.\n\n"
            "É só me mandar sua solicitação! 😉"
        )

    result = await tool_registry.execute(tool_name, args, user_phone)

    match tool_name:

        case "registrar_receita":
            if result.get("erro"):
                return f"⚠️ {result['erro']}"
            return (
                f"💰 *R$ {_fmt(result['valor'])}* recebido!\n"
                f"📝 {result['descricao']}\n"
                f"📈 Total de receitas no mês: *R$ {_fmt(result['total_receitas_mes'])}*"
            )

        case "registrar_gasto":
            if result.get("erro"):
                return f"⚠️ {result['erro']}"

            ben_info = f" ({result['beneficiario']})" if result.get("beneficiario") else ""

            if result.get("tipo") == "credito":
                return (
                    f"✅ *R$ {_fmt(result['valor'])}* registrado\n"
                    f"📝 {result['descricao']}{ben_info}\n"
                    f"💳 Cai na {result['fatura_label']}\n"
                    f"📊 Total da fatura: *R$ {_fmt(result['total_fatura'])}*"
                )

            if result.get("tipo") == "parcelado":
                linhas = "\n".join(
                    f" • {p['fatura_label']}: R$ {_fmt(p['valor'])}"
                    for p in result["parcelas"]
                )
                return (
                    f"✅ *{result['descricao']}*{ben_info} parcelado em {len(result['parcelas'])}x\n"
                    f"💰 Total: *R$ {_fmt(result['valor_total'])}*\n"
                    f"💳 Parcelas:\n{linhas}"
                )

            return (
                f"✅ *R$ {_fmt(result['valor'])}* registrado\n"
                f"📝 {result['descricao']}{ben_info}\n"
                f"📊 Total em {result['categoria']}: *R$ {_fmt(result['total_categoria_mes'])}*"
            )

        case "resumo_mensal":
            total_gastos = result["total_gastos"]
            total_receitas = result["total_receitas"]

            if not result["por_categoria"] and total_receitas == 0:
                return "📭 Nenhuma movimentação financeira registrada este mês."
            saldo = result["saldo"]
            
            linhas = "\n".join(
                f" {cat['category']}: R$ {_fmt(float(cat['total']))} "
                f"({round(float(cat['total']) / total_gastos * 100) if total_gastos > 0 else 0}%)"
                for cat in result["por_categoria"]
            )

            if not linhas:
                linhas = " (Nenhum gasto registrado)"

            status_saldo = "🟢" if saldo >= 0 else "🔴"

            return (
                f"📊 *Resumo do mês*\n\n"
                f"💰 *Receitas:* R$ {_fmt(total_receitas)}\n\n"
                f"*Gastos por Categoria:*\n{linhas}\n\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"💰 Total Receitas: *R$ {_fmt(total_receitas)}*\n"
                f"💸 Total Gastos: *R$ {_fmt(total_gastos)}*\n"
                f"{status_saldo} Saldo: *R$ {_fmt(saldo)}*"
            )

        case "consultar_fatura":
            if not result["gastos"]:
                return f"📭 Nenhum lançamento na {result['fatura']}."
            linhas = "\n".join(
                f" • {g['description']}: R$ {_fmt(float(g['amount']))}"
                for g in result["gastos"][:5]
            )
            return (
                f"💳 *{result['fatura'].title()}*\n"
                f"{linhas}\n\n"
                f"💰 Total: *R$ {_fmt(result['total'])}*"
            )

        case "ultimos_gastos":
            if not result["gastos"]:
                return "📭 Nenhum gasto registrado ainda."
            linhas = "\n".join(
                f" • {g['description']}"
                f"{' (' + g['beneficiario'] + ')' if g.get('beneficiario') else ''} "
                f"({g['category']}): R$ {_fmt(float(g['amount']))}"
                for g in result["gastos"]
            )
            return f"🧾 *Últimos gastos*\n{linhas}"

        case "tendencia_semanal":
            if not result["dias"]:
                return "📭 Nenhum gasto nos últimos 7 dias."
            linhas = "\n".join(
                f" {d['day']}: R$ {_fmt(float(d['total']))}"
                for d in result["dias"]
            )
            return f"📈 *Últimos 7 dias*\n{linhas}\n\n💰 Total: *R$ {_fmt(result['total_semana'])}*"

        case "listar_categoria":
            categoria = result["categoria"]
            gastos = result["gastos"]
            count = result["count"]
            total = result["total"]

            EMOJI = {
                "Alimentação": "🍽️", "Transporte": "🚗", "Moradia": "🏠",
                "Saúde": "💊", "Lazer": "🎉", "Pessoal": "👤",
                "Educação": "📚", "Financeiro": "💳", "Pets": "🐾",
            }
            emoji = EMOJI.get(categoria, "📋")

            from datetime import date
            MESES_PT = {
                1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
                5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
                9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
            }
            hoje = date.today()
            mes_label = f"{MESES_PT[hoje.month]}/{hoje.year}"

            if not gastos:
                return f"{emoji} *{categoria} — {mes_label}*\n\n📭 Nenhum gasto registrado nessa categoria."

            linhas = []
            for g in gastos:
                from datetime import datetime
                data = datetime.fromisoformat(g["created_at"].replace("Z", "+00:00")).strftime("%d/%m")
                linha = f" {data} {g['description']}"
                if g.get("subcategory"):
                    linha += f" · {g['subcategory']}"
                if g.get("beneficiario"):
                    linha += f" · {g['beneficiario']}"
                if g.get("payment_method") == "credito":
                    inst = f" ({g['installment_of']}/{g['installment_total']})" if g.get("installment_of") else ""
                    linha += f"{inst} 💳"
                linha += f" · R$ {_fmt(float(g['amount']))}"
                linhas.append(linha)

            corpo = "\n".join(linhas)
            return (
                f"{emoji} *{categoria} — {mes_label}*\n\n"
                f"{corpo}\n\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"🧾 {count} gasto{'s' if count > 1 else ''} · Total: *R$ {_fmt(total)}*"
            )

        case "definir_limite":
            cat = result["categoria"]
            limite = result["limite"]
            gasto = result["gasto_atual"]
            pct = result["percentual_usado"]
            status = "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
            return (
                f"🎯 Limite de *{cat}* definido: *R$ {_fmt(limite)}/mês*\n\n"
                f"{status} Gasto atual: R$ {_fmt(gasto)} ({pct}% do limite)"
            )

        case "consultar_limite":
            if "historico" in result:
                cat = result["categoria"]
                hist = result["historico"]
                if not hist:
                    return f"📭 Nenhum histórico de limite para {cat}."
                linhas = "\n".join(
                    f" {h['mes_referencia']}: R$ {_fmt(float(h['amount']))}"
                    for h in hist
                )
                return f"📋 *Histórico de limite — {cat}*\n{linhas}"

            if "limites" in result:
                limites = result["limites"]
                if not limites:
                    return "📭 Nenhum limite definido ainda.\n\nTente: _limite alimentação 2000_"
                linhas = "\n".join(
                    f" {l['status']} *{l['categoria']}*: R$ {_fmt(l['gasto_atual'])} / R$ {_fmt(l['limite'])} ({l['percentual_usado']}%)"
                    for l in limites
                )
                return f"🎯 *Seus limites — {result['mes']}*\n{linhas}"

            # Single category
            cat = result["categoria"]
            limite = result.get("limite")
            if not limite:
                return f"📭 Nenhum limite definido para *{cat}*.\n\nTente: _limite {cat.lower()} 1000_"
            gasto = result["gasto_atual"]
            pct = result["percentual_usado"] or 0
            status = "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
            return (
                f"🎯 *{cat}*\n"
                f"{status} R$ {_fmt(gasto)} / R$ {_fmt(limite)} ({pct}%)"
            )

        case "sincronizar_banco":
            return result.get("mensagem", "✅ Sincronização bancária concluída.")

        case "listar_categorias_disponiveis":
            expense_cats = result["expense_categories"]
            income_cats = result["income_categories"]
            sub_cats = result["example_subcategories"]

            response_parts = ["✨ *Categorias e Subcategorias que eu conheço:*\n"]

            if expense_cats:
                response_parts.append("\n*Categorias de Gastos:*")
                response_parts.append(", ".join(expense_cats))
            
            if income_cats:
                response_parts.append("\n*Categorias de Receitas:*")
                response_parts.append(", ".join(income_cats))

            if sub_cats:
                response_parts.append("\n*Exemplos de Subcategorias:*")
                response_parts.append(", ".join(sub_cats))
            
            return "\n".join(response_parts)
        case _:
            return json.dumps(result, ensure_ascii=False, default=str)
# ── Agentic loop ──────────────────────────────────────────────────────────────

async def run(user_phone: str, user_message: str) -> str:
    logger.info("Agent run", extra={"phone": user_phone, "user_msg": user_message[:60]})

    # ── Fast path: simple messages bypass the LLM entirely ───────────────────
    classified = _classify(user_message)
    if classified:
        logger.info("Fast path", extra={"tool": classified["tool"]})
        try:
            reply = await _fast_path(classified["tool"], classified["args"], user_phone)
        except Exception as exc:
            logger.warning("Fast path failed, falling back to LLM", extra={"error": str(exc)})
            classified = None  # Fall through to LLM

    # ── Onboarding: first-time users ──────────────────────────────────────────
    if db.is_new_user(user_phone) and (not classified or classified["tool"] != "ajuda"):
        stripped = user_message.strip().replace("dia", "").strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 28:
            dia = int(stripped)
            dia_corte = dia - 7 if dia > 7 else dia - 7 + 30
            db.save_user_settings(user_phone, dia, dia_corte)
            db.save_message(user_phone, "user", user_message)
            reply = (
                f"✅ Configurado! Sua fatura vence todo dia *{dia}* "
                f"e o corte é dia *{dia_corte}*.\n\n"
                "Agora é só mandar seus gastos! Exemplos:\n"
                "• _almoço 35_\n"
                "• _uber 12,50 crédito_\n"
                "• _resumo_"
            )
            db.save_message(user_phone, "assistant", reply)
            return reply

        db.save_message(user_phone, "user", user_message)
        db.save_message(user_phone, "assistant", ONBOARDING_MSG)
        return ONBOARDING_MSG

    # ── Normal flow ───────────────────────────────────────────────────────────

    # Load conversation context from DB
    history = db.get_history(user_phone)

    # Persist user message
    db.save_message(user_phone, "user", user_message)

    if not classified:
        try:
            response = await call_llm(
                system=SYSTEM,
                history=history,
                message=user_message,
                tools=tool_registry.SCHEMAS,
            )

            if response["type"] == "text":
                reply = response["content"]

            elif response["type"] == "tool_call":
                # Otimização: se for apenas uma chamada de ferramenta, usamos o formatador 
                # do fast_path para economizar uma chamada de resumo do LLM (e créditos).
                if len(response["tool_calls"]) == 1:
                    call = response["tool_calls"][0]
                    try:
                        logger.info(f"Otimização: formatando {call['name']} via fast-path")
                        reply = await _fast_path(call["name"], call["args"], user_phone)
                        db.save_message(user_phone, "assistant", reply)
                        return reply
                    except Exception as exc:
                        logger.warning(f"Falha no formatador fast-path: {exc}")

                tool_results: list[str] = []

                for call in response["tool_calls"]:
                    try:
                        result = await tool_registry.execute(call["name"], call["args"], user_phone)
                        r_args = json.dumps(call["args"], ensure_ascii=False)
                        r_result = json.dumps(result, ensure_ascii=False, default=str)
                        tool_results.append(f"[{call['name']}] args={r_args} resultado={r_result}")
                        logger.info("Tool OK", extra={"tool": call["name"]})
                    except Exception as exc:
                        logger.error("Tool failed", extra={"tool": call["name"], "error": str(exc)})
                        tool_results.append(f"[{call['name']}] ERRO: {exc}")

                separator = "\n"
                final = await call_llm(
                    system=SYSTEM,
                    history=[
                        *history,
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": "Resultados:\n" + separator.join(tool_results)},
                    ],
                    message="Com base nesses resultados, responda ao usuário de forma clara e amigável.",
                    tools=[],
                )
                reply = final["content"]

            else:
                reply = "Resposta inesperada do modelo. Tente novamente."

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Agent error", extra={"error": error_msg, "phone": user_phone})
            if "429" in error_msg or "quota" in error_msg.lower():
                reply = f"⚠️ Limite de uso atingido ou falha na chave.\nDetalhes: {error_msg[:100]}"
            else:
                reply = f"Tive um problema técnico: {error_msg[:100]}... Tente novamente em instantes."

    db.save_message(user_phone, "assistant", reply)
    return reply