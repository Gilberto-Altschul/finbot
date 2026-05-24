# app/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Agent — o cérebro do sistema (Versão Unificada, Corrigida e Produção)
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any
from datetime import date, datetime

import app.database as db
import app.tools as tool_registry
from app.llm import call_llm

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
- "sincronizar transacoes.json" → chamar sincronizar_banco(arquivo="transacoes.json")
- "manicure 50" → gasto de R$ 50,00 em Pessoal
- "almoço 35 03/05" → gasto de R$ 35,00 em Alimentação na data 03/05
- "uber 12,50 03/05 credito" → crédito, R$ 12,50 em Transporte na data 03/05
- "farmacia 2x 50,00 03/05 credito" → crédito parcelado 2x na data 03/05
- "resumo" / "quanto gastei" → chamar resumo_mensal
- "fatura" / "cartão" / "quanto vou pagar" → chamar consultar_fatura
- "últimos gastos" / "histórico" → chamar ultimos_gastos
- "últimos gastos débito" → chamar ultimos_gastos(payment_method="debito")
- "últimos gastos crédito" → chamar ultimos_gastos(payment_method="credito")
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

# ── Classificadores por Expressão Regular (Fast Path Original) ────────────────

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

_SYNC_FILE_RE = re.compile(
    r"^(?:sincronizar|atualizar|banco|importar|ler|processar)\s+(?P<arquivo>[a-zA-Z0-9_\-\.]+\.json)$",
    re.IGNORECASE,
)

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
    "celular": "Moradia", "telefone": "Moradia",
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
    "transações":      "ultimos_gastos",
    "transacoes":      "ultimos_gastos",
    "extrato":         "ultimos_gastos",
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
    "ifood": "Alimentação", "alimentacao": "Alimentação", "refeicao": "Alimentação", "refeição": "Alimentação",
    "almoco": "Alimentação", "almoço": "Alimentação", "janta": "Alimentação", "jantar": "Alimentação",
    "lanche": "Alimentação", "cafe": "Alimentação", "café": "Alimentação", "padaria": "Alimentação",
    "mercadolivre*mk4dolma": "Moradia", "mercadolivre*mile": "Moradia",
    "mercado": "Alimentação", "supermercado": "Alimentação", "pizza": "Alimentação", "hamburguer": "Alimentação",
    "hambúrguer": "Alimentação", "lanchonete": "Alimentação", "cereais": "Alimentação", "hortifruti": "Alimentação",
    "hortifrúti": "Alimentação", "acougue": "Alimentação", "açougue": "Alimentação", "peixaria": "Alimentação",
    "restaurante": "Alimentação", "bbq panamby": "Alimentação", "zaffari morumbi": "Alimentação",
    "uber": "Transporte", "uber moto": "Transporte", "99": "Transporte",
    "gasolina": "Transporte", "posto": "Transporte", "estacionamento": "Transporte", "onibus": "Transporte",
    "metro": "Transporte", "transporte": "Transporte", "taxi": "Transporte", "passagem": "Transporte",
    "manutencao": "Transporte", "oficina": "Transporte", "mecânico": "Transporte", "mecanico": "Transporte",
    "revisão": "Transporte", "revisao": "Transporte", "pneu": "Transporte", "aluguel": "Moradia",
    "condominio": "Moradia", "condomínio": "Moradia", "luz": "Moradia", "água": "Moradia", "agua": "Moradia",
    "internet": "Moradia", "telefone": "Moradia", "celular": "Moradia", "gás": "Moradia", "gas": "Moradia",
    "faxina": "Moradia", "faxineira": "Moradia", "limpeza": "Moradia", "casa": "Moradia", "utensílios": "Moradia",
    "utensilios": "Moradia", "móveis": "Moradia", "moveis": "Moradia", "decoração": "Moradia", "decoracao": "Moradia",
    "reforma": "Moradia", "farmácia": "Saúde", "farmacia": "Saúde", "remédio": "Saúde", "remedio": "Saúde",
    "médico": "Saúde", "medico": "Saúde", "academia": "Saúde", "musculação": "Saúde", "musculacao": "Saúde",
    "suplemento": "Saúde", "suplementos": "Saúde", "whey": "Saúde", "crossfit": "Saúde", "yoga": "Saúde",
    "pilates": "Saúde", "dentista": "Saúde", "consulta": "Saúde", "exame": "Saúde",
    "plano de saúde": "Saúde", "plano saude": "Saúde", "plano saúde": "Saúde", "convenio": "Saúde", "convênio": "Saúde",
    "amazonmktplc*adeolivei": "Saúde", "puravida": "Saúde", "terapeutica": "Saúde",
    "pet": "Pets", "petshop": "Pets", "veterinário": "Pets", "veterinario": "Pets", "racao": "Pets", "ração": "Pets",
    "banho": "Pets", "tosa": "Pets", "netflix": "Lazer", "spotify": "Lazer", "hbo": "Lazer", "max": "Lazer",
    "disney": "Lazer", "prime video": "Lazer", "globoplay": "Lazer", "cinema": "Lazer", "show": "Lazer",
    "ingresso": "Lazer", "viagem": "Lazer", "restaurante": "Lazer", "bar": "Lazer", "social": "Lazer",
    "cerveja": "Lazer", "chopp": "Lazer", "balada": "Lazer", "presente": "Lazer", "gift": "Lazer",
    "lembrancinha": "Lazer", "mimo": "Lazer", "american air*capturere": "Lazer",
    "curso": "Educação", "livro": "Educação", "escola": "Educação",
    "faculdade": "Educação", "udemy": "Educação", "linkedin": "Empresa", "software": "Educação", "api": "Educação",
    "roupa": "Pessoal", "tenis": "Pessoal", "sapato": "Pessoal", "camiseta": "Pessoal", "cabelo": "Pessoal",
    "barbeiro": "Pessoal", "barba": "Pessoal", "salao": "Pessoal", "salão": "Pessoal", "estetica": "Pessoal",
    "estética": "Pessoal", "manicure": "Pessoal", "cea mrb 140 ecpc": "Pessoal", "hope": "Pessoal",
    "oeiras": "Pessoal",
    "seguro": "Financeiro", "tarifa": "Financeiro", "anuidade": "Financeiro", "banco": "Financeiro", "imposto": "Financeiro",
    "taxa": "Financeiro", "twilio": "Empresa",
    "taxa": "Financeiro", "twilio": "Empresa", "google one": "Empresa",
}

def _levenshtein(a: str, b: str) -> int:
    """Calcula a distancia de edicao entre duas strings."""
    if len(a) < len(b): return _levenshtein(b, a)
    if not b: return len(a)
    prev = range(len(b) + 1)
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _infer_category(desc: str) -> str:
    """Infere categoria pela descricao."""
    desc_norm = _normalize(desc)

    # 1. Busca exata
    for keyword, category in _CATEGORIES.items():
        if keyword in desc_norm: return category

    # 2. Busca fuzzy por palavra
    single_keywords = {k: v for k, v in _CATEGORIES.items() if " " not in k}
    for word in desc_norm.split():
        if len(word) < 4: continue
        best_dist = 999
        best_cat = None
        for keyword, category in single_keywords.items():
            if abs(len(word) - len(keyword)) > 2: continue
            dist = _levenshtein(word, keyword)
            if dist < best_dist:
                best_dist = dist
                best_cat = category
        max_dist = 1 if len(word) <= 5 else 2
        if best_dist <= max_dist and best_cat: return best_cat

    return "Outros"


def _split_descricao_beneficiario(desc: str) -> tuple[str, str | None]:
    text = " ".join(desc.strip().split())
    lower = text.lower()

    if " para " in lower:
        i = lower.rfind(" para ")
        descricao = text[:i].strip()
        beneficiario = text[i + 6:].strip()
        if descricao and beneficiario: return descricao, beneficiario

    if lower.startswith("pix "):
        beneficiario = text[4:].strip()
        if beneficiario: return "pix", beneficiario

    return text, None


def _parse_method(raw: str | None) -> str:
    if not raw: return "debito"
    raw = raw.lower().strip()
    if any(w in raw for w in ["créd", "cred", "cartão", "cartao"]): return "credito"
    if "dinheiro" in raw: return "dinheiro"
    return "debito"


def _parse_date(raw: str | None) -> date | None:
    if not raw: return None
    raw = raw.strip().replace("-", "/").replace(".", "/")
    parts = raw.split("/")
    today = date.today()
    try:
        if len(parts) == 2:
            d, m = int(parts[0]), int(parts[1])
            return date(today.year, m, d)
        elif len(parts) == 3:
            d, m = int(parts[0]), int(parts[1])
            y = int(parts[2])
            if y < 100: y += 2000
            return date(y, m, d)
    except ValueError: return None
    return None


def _classify(message: str) -> dict | None:
    msg = message.strip()
    msg_norm = _normalize(msg)

    m_sync_file = _SYNC_FILE_RE.match(msg)
    if m_sync_file:
        return {"tool": "sincronizar_banco", "args": {"arquivo": m_sync_file.group("arquivo")}}
    
    # Classificador para "últimos gastos [débito/crédito]"
    if "ultimos gastos" in msg_norm or "ultimos_gastos" in msg_norm or "histórico" in msg_norm or "historico" in msg_norm:
        # Se o usuário pedir os dois tipos na mesma frase, deixamos para a LLM processar múltiplos tool_calls
        if ("debito" in msg_norm or "débito" in msg_norm) and ("credito" in msg_norm or "crédito" in msg_norm):
            return None

        args = {"payment_method": "debito"}
        if "débito" in msg_norm or "debito" in msg_norm:
            args["payment_method"] = "debito"
        elif "crédito" in msg_norm or "credito" in msg_norm or "cartão" in msg_norm or "cartao" in msg_norm:
            args["payment_method"] = "credito"
        
        # Se a mensagem for apenas "ultimos gastos" ou "ultimos gastos debito", etc.
        return {"tool": "ultimos_gastos", "args": args}

    for keyword, tool_name in _KEYWORD_TOOLS.items():
        if _normalize(keyword) in msg_norm:
            if tool_name == "sincronizar_banco" and len(msg.split()) > 1: continue

            if tool_name == "listar_categoria":
                _CAT_MAP = {
                    "alimenta": "Alimentação", "comida": "Alimentação", "mercado": "Alimentação",
                    "transport": "Transporte", "uber": "Transporte", "carro": "Transporte",
                    "moradia": "Moradia", "aluguel": "Moradia", "casa": "Moradia",
                    "saude": "Saúde", "farmacia": "Saúde", "medico": "Saúde",
                    "lazer": "Lazer", "restaurante": "Lazer", "cinema": "Lazer",
                    "pessoal": "Pessoal", "roupa": "Pessoal", "cabelo": "Pessoal",
                    "educacao": "Educação", "curso": "Educação", "livro": "Educação",
                    "financeiro": "Financeiro", "seguro": "Financeiro", "pets": "Pets", "pet": "Pets", "veterinario": "Pets",
                }
                categoria = None
                for key, cat in _CAT_MAP.items():
                    if key in msg_norm:
                        categoria = cat
                        break
                if categoria: return {"tool": tool_name, "args": {"categoria": categoria}}
                return None
            return {"tool": tool_name, "args": {}}

    m_budget = _BUDGET_RE.match(msg)
    if m_budget:
        cat_raw = _normalize(m_budget.group("categoria"))
        categoria = None
        for key, cat in _BUDGET_CAT_MAP.items():
            if _normalize(key) in cat_raw or cat_raw in _normalize(key):
                categoria = cat
                break
        if not categoria: return None

        valor = float(m_budget.group("valor").replace(",", "."))
        args: dict = {"categoria": categoria, "valor": valor}
        mes_raw = m_budget.group("mes")
        if mes_raw: args["mes"] = mes_raw
        return {"tool": "definir_limite", "args": args}

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

        args = {"valor": valor, "categoria": cat, "descricao": desc_raw}
        if pagador: args["pagador"] = pagador
        if expense_date: args["data"] = expense_date.isoformat()
        return {"tool": "registrar_receita", "args": args}

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

    m = _EXPENSE_RE.match(msg)
    if m:
        desc_raw = m.group("desc").strip()
        beneficiario = m.group("beneficiario").strip() if m.group("beneficiario") else None

        if not beneficiario: descricao, beneficiario = _split_descricao_beneficiario(desc_raw)
        else: descricao = desc_raw

        valor = float(m.group("valor").replace(",", "."))
        method = _parse_method(m.group("method"))
        parc = int(m.group("parcelas_pre") or m.group("parcelas_pos") or 1)
        cat = _infer_category(descricao)
        expense_date = _parse_date(m.group("data"))

        args: dict = {"valor": valor, "categoria": cat, "descricao": descricao, "payment_method": method}
        if expense_date: args["data"] = expense_date.isoformat()
        if beneficiario: args["beneficiario"] = beneficiario
        if method == "credito" and parc > 1: args["parcelas"] = parc
        return {"tool": "registrar_gasto", "args": args}

    return None


def _format_output(result: Any, tool_name: str, user_phone: str) -> str:
    """Aplica o template de layout com emojis e quebras de linhas idênticos aos históricos."""
    if not isinstance(result, dict): return str(result)
    if result.get("erro"): return f"⚠️ {result['erro']}"

    # FORMATADOR DOS RESUMOS DE LIMITES / ORÇAMENTOS
    if result.get("tipo_resposta_estruturada") == "resumo_mensal" or tool_name in ["resumo_mensal", "consultar_gastos_do_mes"]:
        total_gastos = result["total_gastos"]
        total_receitas = result["total_receitas"]
        if not result["por_categoria"] and total_receitas == 0: return "📭 Nenhuma movimentação financeira registrada este mês."
        
        linhas = []
        for cat in result["por_categoria"]:
            tot_cat = float(cat["total"])
            pct = cat.get("percentual_usado") or (round(tot_cat / total_gastos * 100) if total_gastos > 0 else 0)
            status = cat.get("status") or "✅"
            linhas.append(f" {status} *{cat['category']}:* R$ {_fmt(tot_cat)} ({pct}%)")
            
        status_saldo = "🟢" if result["saldo"] >= 0 else "🔴"
        return f"📊 *Resumo do mês*\n\n💰 *Receitas:* R$ {_fmt(total_receitas)}\n\n*Gastos por Categoria:*\n" + "\n".join(linhas) + f"\n\n💸 Total Gastos: *R$ {_fmt(total_gastos)}*\n{status_saldo} Saldo: *R$ {_fmt(result['saldo'])}*"

    # FORMATADOR DE CONSULTA DE FATURAS DETALHADAS (Alinhado com image_3e0bef.png)
    if result.get("tipo_resposta_estruturada") == "consultar_fatura" or tool_name in ["consultar_fatura", "consultar_fatura_atual"]:
        if not result.get("gastos"): return f"📭 Nenhum lançamento na fatura {result.get('fatura', '')}."
        linhas = []
        for g in result["gastos"]:
            inst = f" ({g['installment_of']}/{g['installment_total']})" if g.get("installment_of") else ""
            linhas.append(f" • {g['description']}{inst}: R$ {_fmt(float(g['amount']))}")
        return f"💳 *Fatura {result['fatura'].title()}*\n" + "\n".join(linhas) + f"\n\n💰 Total: *R$ {_fmt(result['total'])}*"

    # FORMATADOR DE LANÇAMENTOS DE GASTOS PARCELADOS (Alinhado com image_3e0c8a.png)
    if result.get("tipo") == "parcelado" or ("parcelas" in result and isinstance(result.get("parcelas"), list)):
        linhas = "\n".join(f" • {p['fatura_label']}: R$ {_fmt(p['valor'])}" for p in result["parcelas"])
        ben = f" ({result['beneficiario']})" if result.get("beneficiario") else ""
        return f"✅ *R$ {_fmt(result['valor_total'])}* registrado\n📝 {result['descricao']}{ben} parcelado em {len(result['parcelas'])}x\n💳 Parcelas:\n{linhas}"

    # FORMATADOR DE LANÇAMENTOS DE CRÉDITO À VISTA
    if result.get("tipo") == "credito" or ("fatura_label" in result and result.get("registrado")):
        ben = f" ({result['beneficiario']})" if result.get("beneficiario") else ""
        return f"✅ *R$ {_fmt(result['valor'])}* registrado\n📝 {result['descricao']}{ben}\n💳 Cai na fatura {result['fatura_label']}\n📈 Total da fatura: *R$ {_fmt(result['total_fatura'])}*"

    # FORMATADOR DE DEBITO À VISTA (Copia fiel das quebras de linha de image_840283.png e image_846ee6.png)
    if result.get("tipo") == "debito" or ("total_categoria_mes" in result and result.get("registrado")):
        ben = f" ({result['beneficiario']})" if result.get("beneficiario") else ""
        return f"✅ *R$ {_fmt(result['valor'])}* registrado\n📝 {result['descricao']}{ben}\n📊 Total em {result['categoria']}: *R$ {_fmt(result['total_categoria_mes'])}*"

    # RECEITAS
    if result.get("tipo") == "receita" or "total_receitas_mes" in result:
        return f"💰 *R$ {_fmt(result['valor'])}* recebido!\n📝 {result['descricao']}\n📈 Total de receitas no mês: *R$ {_fmt(result['total_receitas_mes'])}*"

    # OUTROS MÉTODOS DE CONSULTA
    if "gastos" in result and tool_name == "ultimos_gastos":
        if not result["gastos"]: return "📭 Nenhum gasto registrado ainda."

        linhas = []
        for g in result["gastos"]:
            try:
                # Formata a data de ISO (2026-05-21...) para DD/MM
                dt = datetime.fromisoformat(g["created_at"].replace("Z", "+00:00"))
                data_str = dt.strftime("%d/%m")
            except:
                data_str = "??/??"
            
            desc = g['description']
            if g.get('beneficiario'):
                desc += f" ({g['beneficiario']})"
            
            linhas.append(f" • {data_str} - {desc}: *R$ {_fmt(float(g['amount']))}*")

        titulo = "🧾 *Últimos gastos*"
        if result.get("payment_method"):
            titulo += f" ({result['payment_method'].title()})"
        return f"{titulo}\n" + "\n".join(linhas)

    if "dias" in result and tool_name == "tendencia_semanal":
        linhas = "\n".join(f" {d['day']}: R$ {_fmt(float(d['total']))}" for d in result["dias"])
        return f"📈 *Últimos 7 dias*\n{linhas}\n\n💰 Total: *R$ {_fmt(result['total_semana'])}*"

    if tool_name == "definir_limite" and "limite" in result:
        status = "🔴" if result["percentual_usado"] > 100 else "⚠️" if result["percentual_usado"] >= 80 else "✅"
        return f"🎯 Limite de *{result['categoria']}* definido: *R$ {_fmt(result['limite'])}/mês*\n\n{status} Gasto atual: R$ {_fmt(result['gasto_atual'])} ({result['percentual_usado']}% do limite)"

    # FORMATADOR DE CONSULTA DE LIMITES (Geral e Individual)
    if tool_name == "consultar_limite":
        if "historico" in result:
            linhas = "\n".join([f" • {h['mes_referencia']}: R$ {_fmt(float(h['amount']))}" for h in result["historico"]])
            return f"📈 *Histórico de limites: {result['categoria']}*\n{linhas}"
        
        if "limites" in result:
            linhas = []
            for l in result["limites"]:
                linhas.append(f" {l['status']} *{l['categoria']}:* R$ {_fmt(l['gasto_atual'])} / R$ {_fmt(l['limite'])} ({l['percentual_usado']}%)")
            return f"🎯 *Seus Limites ({result['mes']})*\n\n" + "\n".join(linhas)

        if "limite" in result:
            status = "🔴" if result["percentual_usado"] > 100 else "⚠️" if result["percentual_usado"] >= 80 else "✅"
            return f"🎯 *Limite: {result['categoria']}*\n\n{status} Gasto: R$ {_fmt(result['gasto_atual'])}\n🏁 Meta: R$ {_fmt(result['limite'])}\n📊 Uso: {result['percentual_usado']}%"

    reply = result.get("mensagem") if "mensagem" in result else json.dumps(result, ensure_ascii=False)
    
    # PROTEÇÃO: Se a resposta for muito longa, trunca para o Twilio não rejeitar
    if len(reply) > 1550:
        return reply[:1500] + "\n\n... (resumo longo, verifique seu extrato)"
        
    return reply


async def _fast_path(tool_name: str, args: dict, user_phone: str) -> str:
    if tool_name == "ajuda":
        return (
            "Olá! Sou o *FinBot*, seu assistente financeiro. 😊\n\n"
            "Posso te ajudar a organizar suas finanças registrando o que entra e o que sai.\n\n"
            "*O que você gostaria de fazer?*\n\n"
            "• *Registrar um gasto:* Diga o valor e o que comprou (ex: \"almoço 35\" ou \"uber 12,50\").\n"
            "• *Cartão de Crédito:* Adicione 'crédito' ou 'cartão' (ex: \"ifood 50 crédito\").\n"
            "• *Parcelamento:* Informe as parcelas (ex: \"tênis 300 3x crédito\").\n"
            "• *Registrar uma receita:* Diga o que recebeu (ex: \"recebi 5000 salário\").\n"
            "• *Ver um resumo:* Diga \"resumo\" para ver suas finanças por categoria.\n"
            "• *Consultar a fatura:* Peça \"fatura\" ou \"cartão\" para ver o que vai pagar.\n\n"
            "É só me mandar sua solicitação! 😉"
        )
    result = tool_registry.execute(tool_name, args, user_phone)
    return _format_output(result, tool_name, user_phone)

# ── Loop Agêntico Principal ───────────────────────────────────────────────────

async def run(user_phone: str, user_message: str) -> str:
    logger.info("Agent run", extra={"phone": user_phone, "user_msg": user_message[:60]})

    classified = _classify(user_message)
    if classified:
        try:
            reply = await _fast_path(classified["tool"], classified["args"], user_phone)
            db.save_message(user_phone, "user", user_message)
            db.save_message(user_phone, "assistant", reply)
            return reply
        except Exception as exc:
            logger.warning("Fast path failed, falling back to LLM", extra={"error": str(exc)})
            classified = None  

    if db.is_new_user(user_phone) and (not classified or classified["tool"] != "ajuda"):
        stripped = user_message.strip().replace("dia", "").strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 28:
            dia = int(stripped)
            dia_corte = dia - 7 if dia > 7 else dia - 7 + 30
            db.save_user_settings(user_phone, dia, dia_corte)
            db.save_message(user_phone, "user", user_message)
            reply = f"✅ Configurado! Sua fatura vence todo dia *{dia}* e o corte é dia *{dia_corte}*.\n\nMande seu primeiro gasto!"
            db.save_message(user_phone, "assistant", reply)
            return reply

        db.save_message(user_phone, "user", user_message)
        db.save_message(user_phone, "assistant", ONBOARDING_MSG)
        return ONBOARDING_MSG

    history = db.get_history(user_phone)
    db.save_message(user_phone, "user", user_message)

    try:
        response = await call_llm(system=SYSTEM, history=history, message=user_message, tools=tool_registry.SCHEMAS)
        if response["type"] == "text":
            reply = response["content"]
            db.save_message(user_phone, "assistant", reply)
            return reply
        elif response["type"] == "tool_call":
            if len(response["tool_calls"]) == 1:
                call = response["tool_calls"][0]
                try:
                    args_pt = dict(call["args"])
                    if "category" in args_pt and "categoria" not in args_pt: args_pt["categoria"] = args_pt.pop("category")
                    if "description" in args_pt and "descricao" not in args_pt: args_pt["descricao"] = args_pt.pop("description")
                    
                    reply = await _fast_path(call["name"], args_pt, user_phone)
                    db.save_message(user_phone, "assistant", reply)
                    return reply
                except Exception as e: logger.warning(f"Erro no single tool call: {e}")

            tool_results = []
            last_tool_name = "sincronizar_banco"
            for call in response["tool_calls"]:
                try:
                    args_pt = dict(call["args"])
                    if "category" in args_pt and "categoria" not in args_pt: args_pt["categoria"] = args_pt.pop("category")
                    if "description" in args_pt and "descricao" not in args_pt: args_pt["descricao"] = args_pt.pop("description")
                    
                    last_tool_name = call["name"]
                    result = tool_registry.execute(call["name"], args_pt, user_phone)
                    
                    # Formata cada item inserido para acumular com o layout rico e emojis correto
                    tool_results.append(_format_output(result, call["name"], user_phone))
                except Exception as exc:
                    tool_results.append(f"[{call['name']}] ERRO: {exc}")

            # PROTEÇÃO CORE: Evita o estouro dos 1600 caracteres do Twilio em leituras automáticas de PDFs/Open Finance
            texto_consolidado = "\n".join(tool_results)
            if len(texto_consolidado) > 1500:
                count_sucesso = len([r for r in tool_results if "✅" in r or "💰" in r])
                reply = (
                    f"✨ *Sincronização Concluída com Sucesso!*\n\n"
                    f"📊 Foram processados e inseridos *{count_sucesso} novos lançamentos* encontrados no extrato importado do Open Finance.\n\n"
                    f"💡 Digite *'resumo'* ou *'fatura'* para ver o impacto dessas atualizações no seu orçamento!"
                )
            else:
                reply = texto_consolidado

            db.save_message(user_phone, "assistant", reply)
            return reply

    except Exception as exc:
        logger.error(f"Agent error: {exc}", exc_info=True)
        return "⚠️ Desculpe, tive um erro interno ao processar sua mensagem. Tente novamente em instantes."