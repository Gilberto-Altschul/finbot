# app/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Agent — o cérebro do sistema (Versão Unificada e Corrigida)
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import re
import unicodedata
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
- "estacionamento 10" → gasto de R$ 10,00 em Transporte
- "recebi 5000 salário" → receita de R$ 5.000,00 em Salário
- "receita aluguel 3800" → receita de R$ 3.800,00 em Extra
- "uber 12,50 credito" → gasto de R$ 12,50 em Transporte no crédito
- "tênis 300 3x credito" → crédito parcelado, valor TOTAL R$ 300,00 em 3x de R$ 100,00
- "mercado 180 no cartão" → crédito, R$ 180,00 em Alimentação
- "almoço para João 35" → gasto de R$ 35,00 em Alimentação, beneficiário "João"
- "farmácia 45" → gasto de R$ 45,00 em Saúde
- "contabilidade 500" → gasto de R$ 500,00 em Empresa (subcategoria Contabilidade)
- "das 120" → gasto de R$ 120,00 em Empresa (subcategoria DAS)
- "sincronizar transacoes.json" → chamar sincronizar_banco(arquivo="transacoes.json")
- "manicure 50" → gasto de R$ 50,00 em Pessoal
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
    try:
        val = float(value or 0)
        return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "0,00"

def _normalize(text: str) -> str:
    """Remove acentos e converte para minúsculas para comparação robusta."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).lower()

# ── Classificadores por Expressão Regular (Fast Path) ─────────────────────────

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
    "empresa": "Empresa", "trabalho": "Empresa", "negocio": "Empresa",
    "educacao": "Educação", "educação": "Educação", "curso": "Educação",
    "financeiro": "Financeiro", "seguro": "Financeiro",
}

_EXPENSE_RE = re.compile(
    r"^(?P<desc>[a-zA-ZÀ-ÿ0-9 ]+?)\s+"
    r"(?:(?P<parcelas_pre>\d+)[xX]\s+)?(?P<valor>\d+(?:[.,]\d{1,2})?)(?:\s+(?P<parcelas_pos>\d+)[xX])?(?:\s+(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?))?(?:\s+(?P<method>cr[ée]d(?:ito)?|cart[ãa]o|d[ée]b(?:ito)?|dinheiro))?(?:\s+(?:para|pro|pra)\s+(?P<beneficiario>.+))?$",
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
    "ifood": "Alimentação", "alimentacao": "Alimentação", "refeicao": "Alimentação", "refeição": "Alimentação",
    "almoco": "Alimentação", "almoço": "Alimentação", "janta": "Alimentação", "jantar": "Alimentação",
    "lanche": "Alimentação", "cafe": "Alimentação", "café": "Alimentação", "padaria": "Alimentação",
    "mercado": "Alimentação", "supermercado": "Alimentação", "pizza": "Alimentação", "hamburguer": "Alimentação",
    "hambúrguer": "Alimentação", "lanchonete": "Alimentação", "cereais": "Alimentação", "hortifruti": "Alimentação",
    "hortifrúti": "Alimentação", "acougue": "Alimentação", "açougue": "Alimentação", "peixaria": "Alimentação",
    "restaurante": "Alimentação",

    # Transporte
    "uber": "Transporte", "uber moto": "Transporte", "99": "Transporte", "gasolina": "Transporte",
    "posto": "Transporte", "estacionamento": "Transporte", "onibus": "Transporte", "metro": "Transporte",
    "transporte": "Transporte", "taxi": "Transporte", "passagem": "Transporte", "manutencao": "Transporte", "pedagio": "Transporte", "pedágio": "Transporte",
    "estapar": "Transporte", "zona azul": "Transporte", "tag": "Transporte", "estac": "Transporte",
    "oficina": "Transporte", "mecânico": "Transporte", "mecanico": "Transporte", "revisão": "Transporte",
    "revisao": "Transporte", "pneu": "Transporte",

    # Moradia
    "aluguel": "Moradia", "condominio": "Moradia", "condomínio": "Moradia", "luz": "Moradia",
    "água": "Moradia", "agua": "Moradia", "internet": "Moradia", "telefone": "Moradia",
    "celular": "Moradia", "gás": "Moradia", "gas": "Moradia", "faxina": "Moradia",
    "faxineira": "Moradia", "limpeza": "Moradia", "casa": "Moradia", "utensílios": "Moradia",
    "utensilios": "Moradia", "móveis": "Moradia", "moveis": "Moradia", "decoração": "Moradia",
    "decoracao": "Moradia", "reforma": "Moradia",

    # Saúde
    "farmácia": "Saúde", "farmacia": "Saúde", "remédio": "Saúde", "remedio": "Saúde",
    "médico": "Saúde", "medico": "Saúde", "academia": "Saúde", "musculação": "Saúde",
    "musculacao": "Saúde", "suplemento": "Saúde", "suplementos": "Saúde", "whey": "Saúde",
    "crossfit": "Saúde", "yoga": "Saúde", "pilates": "Saúde", "dentista": "Saúde",
    "consulta": "Saúde", "exame": "Saúde", "plano de saude": "Saúde", "plano de saúde": "Saúde",
    "plano saude": "Saúde", "plano saúde": "Saúde", "convenio": "Saúde", "convênio": "Saúde",

    # Pets
    "pet": "Pets", "petshop": "Pets", "veterinário": "Pets", "veterinario": "Pets",
    "racao": "Pets", "ração": "Pets", "banho": "Pets", "tosa": "Pets",

    # Lazer / Social
    "netflix": "Lazer", "spotify": "Lazer", "hbo": "Lazer", "max": "Lazer",
    "disney": "Lazer", "prime video": "Lazer", "globoplay": "Lazer", "cinema": "Lazer",
    "show": "Lazer", "ingresso": "Lazer", "viagem": "Lazer", "restaurante": "Lazer",
    "bar": "Lazer", "social": "Lazer", "cerveja": "Lazer", "chopp": "Lazer",
    "balada": "Lazer", "presente": "Lazer", "gift": "Lazer", "lembrancinha": "Lazer",
    "mimo": "Lazer",

    # Educação
    "curso": "Educação", "livro": "Educação", "escola": "Educação", "faculdade": "Educação",
    "udemy": "Educação", "linkedin": "Educação", "software": "Educação", "api": "Educação",

    # Vestuário / Beleza -> Pessoal
    "roupa": "Pessoal", "tenis": "Pessoal", "sapato": "Pessoal", "camiseta": "Pessoal",
    "cabelo": "Pessoal", "barbeiro": "Pessoal", "barba": "Pessoal", "salao": "Pessoal",
    "salão": "Pessoal", "estetica": "Pessoal", "estética": "Pessoal", "manicure": "Pessoal",

    # Financeiro
    "seguro": "Financeiro", "tarifa": "Financeiro", "anuidade": "Financeiro", "banco": "Financeiro",
    "imposto": "Financeiro", "taxa": "Financeiro",

    # Empresa
    "empresa": "Empresa", "contabilidade": "Empresa", "das": "Empresa", "mei": "Empresa", "simples nacional": "Empresa"
}

def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b): return _levenshtein(b, a)
    if not b: return len(a)
    prev = range(len(b) + 1)
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b): curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]

def _infer_category(desc: str) -> str:
    desc_norm = _normalize(desc)
    for keyword, category in _CATEGORIES.items():
        if keyword in desc_norm: return category
    single_keywords = {k: v for k, v in _CATEGORIES.items() if " " not in k}
    for word in desc_norm.split():
        if len(word) < 4: continue
        best_dist = 999
        best_cat = None
        for keyword, category in single_keywords.items():
            if abs(len(word) - len(keyword)) > 2: continue
            dist = _levenshtein(word, keyword)
            if dist < best_dist: best_dist = dist; best_cat = category
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
        if len(parts) == 2: return date(today.year, int(parts[1]), int(parts[0]))
        elif len(parts) == 3:
            y = int(parts[2])
            if y < 100: y += 2000
            return date(y, int(parts[1]), int(parts[0]))
    except ValueError: return None
    return None

def _classify(message: str) -> dict | None:
    msg = message.strip()
    msg_norm = _normalize(msg)

    m_sync_file = _SYNC_FILE_RE.match(msg)
    if m_sync_file: return {"tool": "sincronizar_banco", "args": {"arquivo": m_sync_file.group("arquivo")}}

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
                    "financeiro": "Financeiro", "seguro": "Financeiro", "pets": "Pets",
                    "pet": "Pets", "veterinario": "Pets",
                    "empresa": "Empresa", "contabilidade": "Empresa", "das": "Empresa"
                }
                categoria = None
                for key, cat in _CAT_MAP.items():
                    if key in msg_norm: categoria = cat; break
                if categoria: return {"tool": tool_name, "args": {"categoria": categoria}}
                return None
            return {"tool": tool_name, "args": {}}

    m_budget = _BUDGET_RE.match(msg)
    if m_budget:
        cat_raw = _normalize(m_budget.group("categoria"))
        categoria = None
        for key, cat in _BUDGET_CAT_MAP.items():
            if _normalize(key) in cat_raw or cat_raw in _normalize(key): categoria = cat; break
        if not categoria: return None
        valor = float(m_budget.group("valor").replace(",", "."))
        args = {"categoria": categoria, "valor": valor}
        if m_budget.group("mes"): args["mes"] = m_budget.group("mes")
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
    if m_transfer: return {"tool": "registrar_gasto", "args": {"valor": float(m_transfer.group("valor").replace(",", ".")), "categoria": "Outros", "descricao": "transferencia", "beneficiario": m_transfer.group("beneficiario").strip(), "payment_method": "debito"}}

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
        
        # Identificação de subcategoria para Empresa no Fast Path
        subcat = None
        if cat == "Empresa":
            if "contabilidade" in _normalize(descricao): subcat = "Contabilidade"
            elif "das" in _normalize(descricao): subcat = "DAS"

        expense_date = _parse_date(m.group("data"))
        args = {"valor": valor, "categoria": cat, "descricao": descricao, "payment_method": method}
        if expense_date: args["data"] = expense_date.isoformat()
        if beneficiario: args["beneficiario"] = beneficiario
        if method == "credito" and parc > 1: args["parcelas"] = parc
        if subcat: args["subcategoria"] = subcat
        return {"tool": "registrar_gasto", "args": args}
    return None

def _format_output(result: dict, tool_name: str, user_phone: str) -> str:
    """Centralizador de Layout: Renderiza os blocos rich text nativos com as quebras corretas."""
    if not isinstance(result, dict):
        return str(result)
        
    if result.get("erro"):
        return f"⚠️ {result['erro']}"

    match tool_name:
        case "registrar_receita":
            if "valor" not in result: return "⚠️ Erro ao processar registro de receita."
            return (
                f"💰 *R$ {_fmt(result.get('valor', 0.0))}* recebido!\n"
                f"📝 {result.get('descricao', 'Receita')}\n"
                f"📈 Total de receitas no mês: *R$ {_fmt(result.get('total_receitas_mes', 0.0))}*"
            )

        case "registrar_gasto":
            if "valor" not in result and "valor_total" not in result:
                return "⚠️ Gasto registrado, mas houve um erro ao recuperar os totais."
            ben_info = f" ({result['beneficiario']})" if result.get("beneficiario") else ""
            if result.get("tipo") == "credito":
                return (
                    f"✅ *R$ {_fmt(result.get('valor', 0.0))}* registrado\n"
                    f"📝 {result.get('descricao', 'Gasto')}{ben_info}\n"
                    f"💳 Cai na {result.get('fatura_label', 'Fatura')}\n"
                    f"📊 Total da fatura: *R$ {_fmt(result.get('total_fatura', 0.0))}*"
                )
            if result.get("tipo") == "parcelado":
                linhas = "\n".join(f" • {p.get('fatura_label', 'Fatura')}: R$ {_fmt(p.get('valor', 0.0))}" for p in result.get("parcelas", []))
                return (
                    f"✅ *{result.get('descricao', 'Gasto')}*{ben_info} parcelado em {len(result.get('parcelas', []))}x\n"
                    f"💰 Total: *R$ {_fmt(result.get('valor_total', 0.0))}*\n"
                    f"💳 Parcelas:\n{linhas}"
                )
            return (
                f"✅ *R$ {_fmt(result.get('valor', 0.0))}* registrado\n"
                f"📝 {result.get('descricao', 'Gasto')}{ben_info}\n"
                f"📊 Total em {result.get('categoria', 'Transporte')}: *R$ {_fmt(result.get('total_categoria_mes', 0.0))}*"
            )

        case "resumo_mensal":
            total_gastos = result.get("total_gastos", 0.0)
            total_receitas = result.get("total_receitas", 0.0)
            if not result["por_categoria"] and total_receitas == 0:
                return "📭 Nenhuma movimentação financeira registrada este mês."
                
            linhas_list = []
            for cat in result["por_categoria"]:
                tot_cat = float(cat["total"])
                pct_val = round(tot_cat / total_gastos * 100) if total_gastos > 0 else 0
                status_emoji = cat.get("status") or "✅"
                linhas_list.append(f" {status_emoji} *{cat['category']}:* R$ {_fmt(tot_cat)} ({pct_val}%)")

            linhas = "\n".join(linhas_list) if linhas_list else " (Nenhum gasto registrado)"
            status_saldo = "🟢" if result["saldo"] >= 0 else "🔴"
            return (
                f"📊 *Resumo do mês*\n\n"
                f"💰 *Receitas:* R$ {_fmt(total_receitas)}\n\n"
                f"*Gastos por Categoria:*\n{linhas}\n\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"💰 Total Receitas: *R$ {_fmt(total_receitas)}*\n"
                f"💸 Total Gastos: *R$ {_fmt(total_gastos)}*\n"
                f"{status_saldo} Saldo: *R$ {_fmt(result['saldo'])}*"
            )

        case "consultar_fatura":
            if not result["gastos"]: return f"📭 Nenhum lançamento na {result['fatura']}."

            linhas_fatura = []
            for g in result["gastos"]:
                inst = ""
                if g.get("installment_of"):
                    inst = f" ({g['installment_of']}/{g['installment_total']})"
                linhas_fatura.append(f" • {g['description']}{inst}: R$ {_fmt(float(g['amount']))}")
            
            return f"💳 *{result.get('fatura', 'Fatura').title()}*\n" + "\n".join(linhas_fatura) + f"\n\n💰 Total: *R$ {_fmt(result.get('total', 0.0))}*"

        case "ultimos_gastos":
            if not result["gastos"]: return "📭 Nenhum gasto registrado ainda."
            linhas_ultimos = []
            for g in result["gastos"]:
                ben = f" ({g['beneficiario']})" if g.get("beneficiario") else ""
                linhas_ultimos.append(f" • {g['description']}{ben} ({g['category']}): R$ {_fmt(float(g['amount']))}")
            return f"🧾 *Últimos gastos*\n" + "\n".join(linhas_ultimos)

        case "tendencia_semanal":
            if not result["dias"]: return "📭 Nenhum gasto nos últimos 7 dias."
            linhas = "\n".join(f" {d['day']}: R$ {_fmt(float(d['total']))}" for d in result["dias"])
            return f"📈 *Últimos 7 dias*\n{linhas}\n\n💰 Total: *R$ {_fmt(result['total_semana'])}*"

        case "listar_categoria":
            categoria = result["categoria"]
            gastos = result["gastos"]
            EMOJI = {"Alimentação": "🍽️", "Transporte": "🚗", "Moradia": "🏠", "Saúde": "💊", "Lazer": "🎉", "Pessoal": "👤", "Educação": "📚", "Financeiro": "💳", "Pets": "🐾", "Empresa": "🏢"}
            emoji = EMOJI.get(categoria, "📋")
            MESES_PT = {1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"}
            hoje = date.today()
            mes_label = f"{MESES_PT[hoje.month]}/{hoje.year}"
            if not gastos: return f"{emoji} *{categoria} — {mes_label}*\n\n📭 Nenhum gasto registrado nessa categoria."
            linhas = []
            for g in gastos:
                data = datetime.fromisoformat(g["created_at"].replace("Z", "+00:00")).strftime("%d/%m")
                linha = f" {data} {g['description']}"
                if g.get("subcategory"): linha += f" · {g['subcategory']}"
                if g.get("beneficiario"): linha += f" · {g['beneficiario']}"
                if g.get("payment_method") == "credito":
                    inst_info = ""
                    if g.get("installment_of"):
                        inst_info = f" ({g['installment_of']}/{g['installment_total']})"
                    linha += f"{inst_info} 💳"
                linha += f" · R$ {_fmt(float(g['amount']))}"
                linhas.append(linha)
            return f"{emoji} *{categoria} — {mes_label}*\n\n" + "\n".join(linhas) + f"\n\n➖➖➖➖➖➖➖➖➖➖\n🧾 {result['count']} gasto{'s' if result['count'] > 1 else ''} · Total: *R$ {_fmt(result['total'])}*"

        case "definir_limite":
            pct = result["percentual_usado"]
            status = "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
            return f"🎯 Limite de *{result['categoria']}* definido: *R$ {_fmt(result['limite'])}/mês*\n\n{status} Gasto atual: R$ {_fmt(result['gasto_atual'])} ({pct}% do limite)"

        case "consultar_limite":
            if "historico" in result:
                if not result["historico"]: return f"📭 Nenhum histórico de limite para {result['categoria']}."
                linhas = "\n".join(f" {h['mes_referencia']}: R$ {_fmt(float(h['amount']))}" for h in result["historico"])
                return f"📋 *Histórico de limite — {result['categoria']}*\n{linhas}"
            if "limites" in result:
                if not result["limites"]: return f"📭 Nenhum limite definido ainda.\n\nTente: _limite alimentação 2000_"
                linhas = "\n".join(f" {l['status']} *{l['categoria']}*: R$ {_fmt(l['gasto_atual'])} / R$ {_fmt(l['limite'])} ({l['percentual_usado']}%)" for l in result["limites"])
                return f"🎯 *Seus limites — {result['mes']}*\n{linhas}"
            pct = result["percentual_usado"] or 0
            status = "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
            return f"🎯 *{result['categoria']}*\n{status} R$ {_fmt(result['gasto_atual'])} / R$ {_fmt(result['limite'])} ({pct}%)"

        case _:
            return json.dumps(result, ensure_ascii=False, default=str)

async def _fast_path(tool_name: str, args: dict, user_phone: str) -> str:
    if tool_name == "ajuda":
        return (
            "Olá! Sou o *FinBot*, seu assistente financeiro. 😊\n\n"
            "Posso te ajudar a organizar suas finanças registrando o que entra e o que sai.\n\n"
            "*O que você gostaria de fazer?*\n\n"
            "* *Registrar um gasto:* Diga o valor e o que comprou (ex: \"almoço 35\" ou \"uber 12,50\").\n"
            "* *Cartão de Crédito:* Adicione 'crédito' ou 'cartão' (ex: \"ifood 50 crédito\").\n"
            "* *Parcelamento:* Informe as parcelas (ex: \"tênis 300 3x crédito\").\n"
            "* *Registrar uma receita:* Diga o que recebeu (ex: \"recebi 5000 salário\" ou \"pix 100 de João\").\n"
            "* *Ver um resumo:* Diga \"resumo\" para ver suas finanças por categoria.\n"
            "* *Consultar a fatura:* Peça \"fatura\" ou \"cartão\" para ver o que vai pagar.\n"
            "* *Configurar o cartão:* Me diga \"meu cartão vence dia X\" para configurar a data de vencimento.\n\n"
            "É só me mandar sua solicitação! 😉"
        )
    result = tool_registry.execute(tool_name, args, user_phone)
    return _format_output(result, tool_name, user_phone)

# ── Loop Agêntico Principal ───────────────────────────────────────────────────

async def run(user_phone: str, user_message: str) -> str:
    logger.info("Agent run", extra={"phone": user_phone, "user_msg": user_message[:60]})

    classified = _classify(user_message)
    if classified:
        logger.info("Fast path", extra={"tool": classified["tool"]})
        try:
            reply = await _fast_path(classified["tool"], classified["args"], user_phone)
            db.save_message(user_phone, "user", user_message)
            db.save_message(user_phone, "assistant", reply)
            return reply
        except Exception as exc:
            logger.warning(f"Fast path failed ({exc}), falling back to LLM", exc_info=True)
            classified = None

    if db.is_new_user(user_phone) and (not classified or classified["tool"] != "ajuda"):
        stripped = user_message.strip().replace("dia", "").strip()
        if stripped.isdigit() and 1 <= int(stripped) <= 28:
            dia = int(stripped)
            dia_corte = dia - 7 if dia > 7 else dia - 7 + 30
            db.save_user_settings(user_phone, dia, dia_corte)
            db.save_message(user_phone, "user", user_message)
            reply = f"✅ Configurado! Sua fatura vence todo dia *{dia}* e o corte é dia *{dia_corte}*.\n\nAgora é só mandar seus gastos!"
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
        elif response["type"] == "tool_call":
            if len(response["tool_calls"]) == 1:
                call = response["tool_calls"][0]
                try:
                    reply = await _fast_path(call["name"], call["args"], user_phone)
                    db.save_message(user_phone, "assistant", reply)
                    return reply
                except Exception as e: logger.warning(f"Erro no single tool call: {e}")

            tool_results = []
            last_tool_name = "registrar_gasto"
            last_result = {}
            for call in response["tool_calls"]:
                try:
                    last_tool_name = call["name"]
                    last_result = tool_registry.execute(call["name"], call["args"], user_phone)
                    tool_results.append(json.dumps(last_result, ensure_ascii=False))
                except Exception as exc:
                    tool_results.append(f"[{call['name']}] ERRO: {exc}")

            reply = _format_output(last_result, last_tool_name, user_phone)
    except Exception as exc:
        logger.error(f"Agent error: {exc}", exc_info=True)
        reply = "Tive um problema técnico temporário ao processar sua mensagem. Tente novamente em instantes."

    db.save_message(user_phone, "assistant", reply)
    return reply