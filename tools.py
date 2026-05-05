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
from typing import Any

import database as db

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
            },
            "required": ["valor", "categoria", "descricao"],
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
    logger.info("Tool called", extra={"tool": name, "args": args})

    match name:
        case "registrar_gasto":
            valor: float = args.get("valor", 0)
            categoria: str = args["categoria"]
            descricao: str = args["descricao"]

            if valor <= 0:
                return {"erro": "Valor inválido. Informe um valor positivo."}

            row = db.save_expense(user_phone, valor, categoria, descricao)
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

        case _:
            raise ValueError(f"Ferramenta desconhecida: {name}")
