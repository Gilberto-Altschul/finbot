# tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Every capability the agent has lives here.
#
# Each tool has:
#   schema   → sent to the LLM (OpenAI-compatible function calling format)
#   handler  → the actual Python code that runs when the LLM calls the tool
#
# To add a new capability: add to SCHEMAS + add a branch in execute().
# The LLM will start using it automatically.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date
from typing import Any

import database as db
from billing import fatura_vencimento, fatura_label, parcelas

logger = logging.getLogger(__name__)

# ── Tool schemas ──────────────────────────────────────────────────────────────

SCHEMAS: list[dict] = [
    {
        "name": "registrar_gasto",
        "description": (
            "Registra um gasto do usuário. "
            "Use sempre que o usuário mencionar uma despesa, compra, gasto ou pagamento. "
            "Infira a categoria a partir da descrição quando não for explícita."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {
                    "type": "number",
                    "description": "Valor em reais. Converta vírgula para ponto (ex: '12,50' → 12.5)",
                },
                "categoria": {
                    "type": "string",
                    "enum": [
                        "Alimentação", "Transporte", "Moradia",
                        "Saúde", "Lazer", "Educação", "Vestuário", "Outros",
                    ],
                    "description": "Categoria mais adequada para o gasto",
                },
                "descricao": {
                    "type": "string",
                    "description": "Descrição curta do gasto (ex: 'almoço', 'uber', 'conta de luz')",
                },
                "payment_method": {
                    "type": "string",
                    "enum": ["debito", "credito", "dinheiro"],
                    "description": "Meio de pagamento. Padrão: debito. Use 'credito' se mencionar cartão.",
                },
                "parcelas": {
                    "type": "integer",
                    "description": "Número de parcelas. Só para crédito. Omitir se não for parcelado.",
                },
            },
            "required": ["valor", "categoria", "descricao"],
        },
    },
    {
        "name": "configurar_cartao",
        "description": (
            "Salva o dia de vencimento do cartão de crédito do usuário. "
            "Use quando o usuário informar o dia de vencimento da fatura."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dia_vencimento": {
                    "type": "integer",
                    "description": "Dia do mês em que a fatura vence (1-28)",
                },
            },
            "required": ["dia_vencimento"],
        },
    },
    {
        "name": "consultar_fatura",
        "description": (
            "Mostra os lançamentos e o total de uma fatura do cartão de crédito. "
            "Use quando o usuário perguntar sobre a fatura, o cartão ou o que vai pagar. "
            "Se não especificar mês, usa a próxima fatura a vencer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mes": {
                    "type": "string",
                    "description": "Mês da fatura no formato 'YYYY-MM' (ex: '2026-06'). Omitir para próxima fatura.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "resumo_mensal",
        "description": (
            "Retorna o resumo de gastos do mês atual por categoria. "
            "Use quando o usuário pedir: resumo, relatório, quanto gastou, como estão as finanças."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "total_categoria",
        "description": "Retorna o total gasto em uma categoria específica no mês atual.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "description": "Nome da categoria (ex: Alimentação, Transporte)",
                },
            },
            "required": ["categoria"],
        },
    },
    {
        "name": "ultimos_gastos",
        "description": (
            "Lista os gastos mais recentes. "
            "Use quando o usuário pedir histórico, últimos gastos ou o que registrou."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "quantidade": {
                    "type": "integer",
                    "description": "Quantos gastos retornar (padrão 5, máximo 10)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "tendencia_semanal",
        "description": (
            "Mostra a evolução dos gastos nos últimos 7 dias. "
            "Use quando o usuário perguntar sobre tendências ou comparações recentes."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def execute(name: str, args: dict, user_phone: str) -> dict[str, Any]:
    logger.info("Tool called", extra={"tool": name, "tool_args": args})

    match name:
        case "registrar_gasto":
            valor: float = args.get("valor", 0)
            categoria: str = args["categoria"]
            descricao: str = args["descricao"]
            method: str = args.get("payment_method", "debito")
            n_parcelas: int = int(args.get("parcelas") or 1)

            if valor <= 0:
                return {"erro": "Valor inválido. Informe um valor positivo."}

            if method == "credito":
                dia_corte, dia_vencimento = db.get_card_settings(user_phone)
                today = date.today()

                if n_parcelas > 1:
                    plano = parcelas(today, valor, n_parcelas, dia_corte, dia_vencimento)
                    for p in plano:
                        db.save_expense_credit(
                            user_phone=user_phone,
                            amount=p["valor"],
                            category=categoria,
                            description=f"{descricao} ({p['parcela']}/{p['total_parcelas']})",
                            installment_of=p["parcela"],
                            installment_total=p["total_parcelas"],
                        )
                    return {
                        "registrado": True,
                        "tipo": "parcelado",
                        "descricao": descricao,
                        "valor_total": valor,
                        "parcelas": plano,
                    }
                else:
                    due = fatura_vencimento(today, dia_corte, dia_vencimento)
                    db.save_expense_credit(user_phone=user_phone, amount=valor, category=categoria, description=descricao)
                    return {
                        "registrado": True,
                        "tipo": "credito",
                        "valor": valor,
                        "categoria": categoria,
                        "descricao": descricao,
                        "fatura_vencimento": due.isoformat(),
                        "fatura_label": fatura_label(due),
                        "total_fatura": db.fatura_total(user_phone, due.isoformat(), dia_corte),
                    }

            # débito ou dinheiro — comportamento original
            row = db.save_expense(user_phone, valor, categoria, descricao)
            logger.info(f"db.save_expense returned: {row}")
            total_categoria = db.category_total(user_phone, categoria)
            total_mes = db.monthly_total(user_phone)

            return {
                "registrado": True,
                "id": row.get("id"),
                "valor": valor,
                "categoria": categoria,
                "descricao": descricao,
                "total_categoria_mes": total_categoria,
                "total_mes": total_mes,
            }

        case "resumo_mensal":
            por_categoria = db.monthly_by_category(user_phone)
            total = db.monthly_total(user_phone)
            return {"por_categoria": por_categoria, "total": total}

        case "total_categoria":
            categoria = args["categoria"]
            total = db.category_total(user_phone, categoria)
            return {"categoria": categoria, "total": total}

        case "ultimos_gastos":
            quantidade = min(int(args.get("quantidade", 5)), 10)
            gastos = db.recent_expenses(user_phone, quantidade)
            return {"gastos": gastos}

        case "tendencia_semanal":
            dias = db.daily_trend(user_phone, 7)
            total_semana = round(sum(float(d["total"]) for d in dias), 2)
            return {"dias": dias, "total_semana": total_semana}

        case "configurar_cartao":
            dia_vencimento = int(args["dia_vencimento"])
            if not 1 <= dia_vencimento <= 28:
                return {"erro": "Dia de vencimento deve ser entre 1 e 28."}
            dia_corte = dia_vencimento - 7 if dia_vencimento > 7 else dia_vencimento - 7 + 30
            db.save_user_settings(user_phone, dia_vencimento, dia_corte)
            return {
                "configurado": True,
                "dia_vencimento": dia_vencimento,
                "dia_corte": dia_corte,
            }

        case "consultar_fatura":
            dia_corte, dia_vencimento = db.get_card_settings(user_phone)
            mes = args.get("mes")
            if mes:
                year, month = int(mes[:4]), int(mes[5:7])
                from calendar import monthrange as _mr
                last_day = _mr(year, month)[1]
                due = date(year, month, min(dia_vencimento, last_day))
            else:
                due = fatura_vencimento(date.today(), dia_corte, dia_vencimento)
            gastos = db.expenses_by_fatura(user_phone, due.isoformat(), dia_corte)
            total = round(sum(float(g["amount"]) for g in gastos), 2)
            return {
                "fatura": fatura_label(due),
                "vencimento": due.isoformat(),
                "total": total,
                "gastos": gastos,
            }

        case _:
            raise ValueError(f"Ferramenta desconhecida: {name}")
