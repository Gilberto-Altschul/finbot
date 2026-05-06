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

import database as db
import tools as tool_registry
from llm import call_llm

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM = """
Você é o FinBot, um assistente financeiro pessoal via WhatsApp.
Seu objetivo: ajudar o usuário a registrar gastos e entender seus hábitos de consumo.

PERSONALIDADE:
- Direto, amigável e sem enrolação (é WhatsApp, não email)
- Use emojis com moderação para deixar as mensagens mais legíveis
- Responda sempre em português do Brasil

REGRAS IMPORTANTES:
- Registre gastos SEM pedir confirmação — o usuário quer agilidade
- Sempre informe o total da categoria após registrar um gasto
- Ao mostrar valores, use formato R$ X.XXX,XX
- Se o usuário escrever algo ambíguo, interprete pelo contexto e aja
- Nunca invente dados financeiros — use apenas os dados das ferramentas
- Se o usuário pedir algo fora do escopo financeiro, redirecione com gentileza

INTERPRETAÇÃO DE MENSAGENS:
- "almoço 35" → gasto de R$ 35,00 em Alimentação
- "uber 12,50" → gasto de R$ 12,50 em Transporte
- "uber 12,50 crédito" → gasto de R$ 12,50 em Transporte no crédito
- "tênis 300 3x crédito" → crédito parcelado, R$ 300,00 em 3x
- "mercado 180 no cartão" → crédito, R$ 180,00 em Alimentação
- "farmácia 45" → gasto de R$ 45,00 em Saúde
- "resumo" / "quanto gastei" → chamar resumo_mensal
- "fatura" / "cartão" / "quanto vou pagar" → chamar consultar_fatura
- "últimos gastos" / "histórico" → chamar ultimos_gastos
- "meu cartão vence dia X" → chamar configurar_cartao

CARTÃO DE CRÉDITO:
- Ao confirmar gasto no crédito, sempre mostre em qual fatura vai cair
- Parcelado: mostre o valor de cada parcela e em quais faturas vão cair

FORMATO DAS RESPOSTAS:
- Gasto débito: valor, categoria, descrição e total da categoria no mês
- Gasto crédito: valor, categoria, fatura que vai cair (ex: "fatura junho/2026")
- Gasto parcelado: valor total, valor de cada parcela e em qual fatura cai
- Fatura: total e lista dos principais lançamentos
- Resumo: cada categoria com valor e percentual, depois total geral
- Seja conciso — o usuário está no celular
""".strip()

ONBOARDING_MSG = (
    "👋 Olá! Sou o *FinBot*, seu assistente de controle financeiro.\n\n"
    "Para começar, me diz: *qual o dia de vencimento da sua fatura do cartão de crédito?*\n\n"
    "_(Se não usar cartão, pode digitar qualquer dia — ex: 1)_"
)


# ── Complexity classifier ─────────────────────────────────────────────────────
# Simple messages are handled with regex + direct tool call — zero LLM cost.
# Complex messages go through the full agentic loop.

# Matches: "almoço 35", "uber 12,50", "mercado 180,00 crédito", "ifood 42 3x crédito"
_EXPENSE_RE = re.compile(
    r"^(?P<desc>[a-záàâãéêíóôõúçA-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s\d]+?)\s+"
    r"(?P<valor>\d+(?:[.,]\d{1,2})?)"
    r"(?:\s+(?P<parcelas>\d+)x)?"
    r"(?:\s+(?P<method>créd(?:ito)?|cartão|déb(?:ito)?|dinheiro))?$",
    re.IGNORECASE,
)

_KEYWORD_TOOLS: dict[str, str] = {
    "resumo":          "resumo_mensal",
    "relatório":       "resumo_mensal",
    "quanto gastei":   "resumo_mensal",
    "minhas finanças": "resumo_mensal",
    "fatura":          "consultar_fatura",
    "cartão":          "consultar_fatura",
    "quanto vou pagar":"consultar_fatura",
    "últimos gastos":  "ultimos_gastos",
    "historico":       "ultimos_gastos",
    "histórico":       "ultimos_gastos",
    "o que registrei": "ultimos_gastos",
    "tendência":       "tendencia_semanal",
    "essa semana":     "tendencia_semanal",
}

_CATEGORIES = {
    "ifood": "Alimentação", "restaurante": "Alimentação", "almoço": "Alimentação",
    "janta": "Alimentação", "jantar": "Alimentação", "lanche": "Alimentação",
    "café": "Alimentação", "padaria": "Alimentação", "mercado": "Alimentação",
    "supermercado": "Alimentação", "pizza": "Alimentação", "hamburguer": "Alimentação",
    "uber": "Transporte", "99": "Transporte", "gasolina": "Transporte",
    "estacionamento": "Transporte", "ônibus": "Transporte", "metro": "Transporte",
    "metrô": "Transporte", "transporte": "Transporte", "taxi": "Transporte",
    "táxi": "Transporte", "passagem": "Transporte",
    "aluguel": "Moradia", "condomínio": "Moradia", "luz": "Moradia",
    "água": "Moradia", "internet": "Moradia", "telefone": "Moradia",
    "farmácia": "Saúde", "remédio": "Saúde", "médico": "Saúde",
    "academia": "Saúde", "dentista": "Saúde",
    "netflix": "Lazer", "spotify": "Lazer", "cinema": "Lazer",
    "show": "Lazer", "ingresso": "Lazer", "viagem": "Lazer",
    "curso": "Educação", "livro": "Educação", "escola": "Educação",
    "faculdade": "Educação", "udemy": "Educação",
    "roupa": "Vestuário", "tênis": "Vestuário", "sapato": "Vestuário",
    "camiseta": "Vestuário",
}


def _infer_category(desc: str) -> str:
    desc_lower = desc.lower()
    for keyword, category in _CATEGORIES.items():
        if keyword in desc_lower:
            return category
    return "Outros"


def _parse_method(raw: str | None) -> str:
    if not raw:
        return "debito"
    raw = raw.lower()
    if any(w in raw for w in ["créd", "cartão"]):
        return "credito"
    if "dinheiro" in raw:
        return "dinheiro"
    return "debito"


def _classify(message: str) -> dict | None:
    """
    Try to classify the message without an LLM.
    Returns a dict with tool + args if simple, or None if complex.
    """
    msg = message.strip()
    msg_lower = msg.lower()

    # Keyword-based tool dispatch
    for keyword, tool_name in _KEYWORD_TOOLS.items():
        if keyword in msg_lower:
            return {"tool": tool_name, "args": {}}

    # Expense pattern
    m = _EXPENSE_RE.match(msg)
    if m:
        desc   = m.group("desc").strip()
        valor  = float(m.group("valor").replace(",", "."))
        method = _parse_method(m.group("method"))
        parc   = int(m.group("parcelas")) if m.group("parcelas") else 1
        cat    = _infer_category(desc)

        args: dict = {"valor": valor, "categoria": cat, "descricao": desc, "payment_method": method}
        if method == "credito" and parc > 1:
            args["parcelas"] = parc

        return {"tool": "registrar_gasto", "args": args}

    return None  # Complex — needs LLM


async def _fast_path(tool_name: str, args: dict, user_phone: str) -> str:
    """Execute a tool directly and return a formatted reply without calling the LLM."""
    result = await tool_registry.execute(tool_name, args, user_phone)

    match tool_name:
        case "registrar_gasto":
            if result.get("tipo") == "credito":
                return (
                    f"✅ *R$ {result['valor']:.2f}* registrado em *{result['categoria']}*\n"
                    f"💳 Cai na {result['fatura_label']}\n"
                    f"📊 Total da fatura: *R$ {result['total_fatura']:.2f}*"
                )
            if result.get("tipo") == "parcelado":
                linhas = "\n".join(
                    f"  • {p['fatura_label']}: R$ {p['valor']:.2f}"
                    for p in result["parcelas"]
                )
                return (
                    f"✅ *{result['descricao']}* parcelado em {len(result['parcelas'])}x\n"
                    f"💳 Parcelas:\n{linhas}"
                )
            return (
                f"✅ *R$ {result['valor']:.2f}* em *{result['categoria']}*\n"
                f"📝 {result['descricao']}\n"
                f"📊 Total {result['categoria']} no mês: *R$ {result['total_categoria_mes']:.2f}*"
            )

        case "resumo_mensal":
            if not result["por_categoria"]:
                return "📭 Nenhum gasto registrado este mês."
            total = result["total"]
            linhas = "\n".join(
                f"  {cat['category']}: R$ {float(cat['total']):.2f} "
                f"({round(float(cat['total'])/total*100)}%)"
                for cat in result["por_categoria"]
            )
            return f"📊 *Resumo do mês*\n{linhas}\n\n💰 Total: *R$ {total:.2f}*"

        case "consultar_fatura":
            if not result["gastos"]:
                return f"📭 Nenhum lançamento na {result['fatura']}."
            linhas = "\n".join(
                f"  • {g['description']}: R$ {float(g['amount']):.2f}"
                for g in result["gastos"][:5]
            )
            return (
                f"💳 *{result['fatura'].title()}*\n"
                f"{linhas}\n\n"
                f"💰 Total: *R$ {result['total']:.2f}*"
            )

        case "ultimos_gastos":
            if not result["gastos"]:
                return "📭 Nenhum gasto registrado ainda."
            linhas = "\n".join(
                f"  • {g['description']} ({g['category']}): R$ {float(g['amount']):.2f}"
                for g in result["gastos"]
            )
            return f"🧾 *Últimos gastos*\n{linhas}"

        case "tendencia_semanal":
            if not result["dias"]:
                return "📭 Nenhum gasto nos últimos 7 dias."
            linhas = "\n".join(f"  {d['day']}: R$ {float(d['total']):.2f}" for d in result["dias"])
            return f"📈 *Últimos 7 dias*\n{linhas}\n\n💰 Total: *R$ {result['total_semana']:.2f}*"

        case _:
            return json.dumps(result, ensure_ascii=False, default=str)


# ── Agentic loop ──────────────────────────────────────────────────────────────

async def run(user_phone: str, user_message: str) -> str:
    logger.info("Agent run", extra={"phone": user_phone, "user_msg": user_message[:60]})

    # ── Onboarding: first-time users ──────────────────────────────────────────
    if db.is_new_user(user_phone):
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

    # ── Fast path: simple messages bypass the LLM entirely ───────────────────
    classified = _classify(user_message)
    if classified:
        logger.info("Fast path", extra={"tool": classified["tool"]})
        try:
            reply = await _fast_path(classified["tool"], classified["args"], user_phone)
        except Exception as exc:
            logger.warning("Fast path failed, falling back to LLM", extra={"error": str(exc)})
            classified = None  # Fall through to LLM

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
            logger.error("Agent error", extra={"error": str(exc), "phone": user_phone})
            reply = "Tive um problema tecnico agora. Tente novamente em instantes."

    db.save_message(user_phone, "assistant", reply)
    return reply
