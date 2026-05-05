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
- "mercado 180" → gasto de R$ 180,00 em Alimentação
- "farmácia 45" → gasto de R$ 45,00 em Saúde
- "resumo" / "quanto gastei" → chamar resumo_mensal
- "últimos gastos" / "histórico" → chamar ultimos_gastos

FORMATO DAS RESPOSTAS:
- Confirmação de gasto: valor, categoria, descrição e total da categoria no mês
- Resumo: cada categoria com valor e percentual, depois total geral
- Seja conciso — o usuário está no celular
""".strip()


# ── Agentic loop ──────────────────────────────────────────────────────────────

async def run(user_phone: str, user_message: str) -> str:
    logger.info("Agent run", extra={"phone": user_phone, "msg": user_message[:60]})

    # Load conversation context from DB
    history = db.get_history(user_phone)

    # Persist user message
    db.save_message(user_phone, "user", user_message)

    try:
        # ── Step 1: LLM decides what to do ───────────────────────────────────
        response = await call_llm(
            system=SYSTEM,
            history=history,
            message=user_message,
            tools=tool_registry.SCHEMAS,
        )

        if response["type"] == "text":
            reply = response["content"]

        elif response["type"] == "tool_call":
            # ── Step 2: Execute all requested tools ───────────────────────────
            tool_results: list[str] = []

            for call in response["tool_calls"]:
                try:
                    result = await tool_registry.execute(call["name"], call["args"], user_phone)
                    tool_results.append(
                        f"[{call['name']}] args={json.dumps(call['args'], ensure_ascii=False)} "
                        f"resultado={json.dumps(result, ensure_ascii=False, default=str)}"
                    )
                    logger.info("Tool OK", extra={"tool": call["name"]})
                except Exception as exc:
                    logger.error("Tool failed", extra={"tool": call["name"], "error": str(exc)})
                    tool_results.append(f"[{call['name']}] ERRO: {exc}")

            # ── Step 3: LLM formats the final user-facing response ────────────
            final = await call_llm(
                system=SYSTEM,
                history=[
                    *history,
                    {"role": "user", "content": user_message},
                    {
                        "role": "assistant",
                        "content": (
                            "Executei as ferramentas necessárias. Resultados:\n"
                            + "\n".join(tool_results)
                        ),
                    },
                ],
                message="Com base nesses resultados, responda ao usuário de forma clara e amigável.",
                tools=[],  # No tools in final step — just format and reply
            )
            reply = final["content"]

        else:
            reply = "⚠️ Resposta inesperada do modelo. Tente novamente."

    except Exception as exc:
        logger.error("Agent error", extra={"error": str(exc), "phone": user_phone})
        reply = "⚠️ Tive um problema técnico agora. Tente novamente em instantes."

    # Persist agent reply
    db.save_message(user_phone, "assistant", reply)

    return reply
