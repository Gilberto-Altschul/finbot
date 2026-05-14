# database.py
# ─────────────────────────────────────────────────────────────────────────────
# Supabase persistence layer.
#
# Tables (create via Supabase dashboard or SQL editor):
#
#   finbot_expenses
#     id          bigint generated always as identity primary key
#     user_phone  text not null
#     amount      numeric(10,2) not null check (amount > 0)
#     category    text not null
#     transaction_type text default 'expense'
#     subcategory text
#     description text not null
#     beneficiario text
#     payment_method text not null default 'debito'
#     installment_of integer
#     installment_total integer
#     created_at  timestamptz default now()
#
#   finbot_conversation
#     id          bigint generated always as identity primary key
#     user_phone  text not null
#     role        text not null check (role in ('user','assistant'))
#     content     text not null
#     created_at  timestamptz default now()
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from supabase import create_client, Client
from config import get_settings

import logging

logger = logging.getLogger(__name__)


@lru_cache
def get_db() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)


# ── User settings ─────────────────────────────────────────────────────────────

def get_user_settings(user_phone: str) -> dict | None:
    """Returns the user's saved settings or None if first time."""
    result = (
        get_db()
        .table("finbot_user_settings")
        .select("*")
        .eq("user_phone", user_phone)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def save_user_settings(user_phone: str, dia_vencimento: int, dia_corte: int) -> dict:
    """Upsert user card settings."""
    row = {
        "user_phone": user_phone,
        "cartao_dia_vencimento": dia_vencimento,
        "cartao_dia_corte": dia_corte,
    }
    result = (
        get_db()
        .table("finbot_user_settings")
        .upsert(row, on_conflict="user_phone")
        .execute()
    )
    return result.data[0]


def is_new_user(user_phone: str) -> bool:
    return get_user_settings(user_phone) is None


def get_card_settings(user_phone: str) -> tuple[int, int]:
    """Returns (dia_corte, dia_vencimento) — from DB or global fallback."""
    settings = get_user_settings(user_phone)
    if settings:
        return settings["cartao_dia_corte"], settings["cartao_dia_vencimento"]
    s = get_settings()
    return s.cartao_dia_corte, s.cartao_dia_vencimento


# ── Expenses ──────────────────────────────────────────────────────────────────

def save_expense(
    user_phone,
    amount,
    category,
    description,
    beneficiario=None,
    subcategoria=None,
    expense_date=None,
    transaction_type="expense",
):
    from datetime import date as _date
    row_data = {
        "user_phone": user_phone,
        "amount": amount,
        "category": category,
        "subcategory": subcategoria,
        "description": description,
        "beneficiario": beneficiario,
        "payment_method": "debito",
        "transaction_type": transaction_type,
    }
    if expense_date and expense_date != _date.today():
        # Salva ao meio-dia UTC para evitar que o fuso horário mude o dia.
        # As funções SQL comparam apenas a DATE (created_at::DATE), ignorando a hora.
        row_data["created_at"] = f"{expense_date.isoformat()}T12:00:00Z"
    row = get_db().table("finbot_expenses").insert(row_data).execute()
    if row.data:
        return row.data[0]
    return {}


def save_expense_credit(
    user_phone,
    amount,
    category,
    description,
    beneficiario=None,
    subcategoria=None,
    installment_of=None,
    installment_total=None,
    expense_date=None,
):
    from datetime import date as _date
    row_data = {
        "user_phone": user_phone,
        "amount": amount,
        "category": category,
        "subcategory": subcategoria,
        "description": description,
        "beneficiario": beneficiario,
        "payment_method": "credito",
        "installment_of": installment_of,
        "installment_total": installment_total,
        "transaction_type": "expense",
    }
    if expense_date and expense_date != _date.today():
        # Salva ao meio-dia UTC para evitar que o fuso horário mude o dia.
        # As funções SQL comparam apenas a DATE (created_at::DATE), ignorando a hora.
        row_data["created_at"] = f"{expense_date.isoformat()}T12:00:00Z"
    row = get_db().table("finbot_expenses").insert(row_data).execute()
    if row.data:
        return row.data[0]
    return {}


def monthly_by_category(user_phone: str) -> list[dict]:
    """Total per category for the current calendar month."""
    result = get_db().rpc(
        "expenses_by_category",
        {"p_phone": user_phone},
    ).execute()
    return result.data or []


def monthly_total(user_phone: str) -> float:
    result = get_db().rpc(
        "expenses_monthly_total",
        {"p_phone": user_phone},
    ).execute()
    return float(result.data or 0)


def monthly_income_total(user_phone: str) -> float:
    result = get_db().rpc(
        "income_monthly_total",
        {"p_phone": user_phone},
    ).execute()
    return float(result.data or 0)


def category_total(user_phone: str, category: str) -> float:
    result = get_db().rpc(
        "expenses_category_total",
        {"p_phone": user_phone, "p_category": category},
    ).execute()
    return float(result.data or 0)


def recent_expenses(user_phone: str, limit: int = 5) -> list[dict]:
    result = (
        get_db()
        .table("finbot_expenses")
        .select("amount, category, description, beneficiario, created_at")
        .eq("user_phone", user_phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def daily_trend(user_phone: str, days: int = 7) -> list[dict]:
    result = get_db().rpc(
        "expenses_daily_trend",
        {"p_phone": user_phone, "p_days": days},
    ).execute()
    return result.data or []


def expenses_by_fatura(user_phone: str, due_date: str, dia_corte: int) -> list[dict]:
    """All credit expenses for a given invoice due date."""
    result = get_db().rpc(
        "expenses_by_fatura",
        {
            "p_phone": user_phone,
            "p_due_date": due_date,
            "p_corte_day": dia_corte,
        },
    ).execute()
    return result.data or []


def fatura_total(user_phone: str, due_date: str, dia_corte: int) -> float:
    rows = expenses_by_fatura(user_phone, due_date, dia_corte)
    return round(sum(float(r["amount"]) for r in rows), 2)



def category_expenses_detail(user_phone: str, category: str, limit: int = 50) -> list[dict]:
    """Lista todos os gastos de uma categoria no mes atual com detalhes."""
    result = get_db().rpc(
        "expenses_by_category_detail",
        {"p_phone": user_phone, "p_category": category, "p_limit": limit},
    ).execute()
    return result.data or []


# ── Budgets ───────────────────────────────────────────────────────────────────

def save_budget(user_phone: str, category: str, amount: float, mes_referencia: str) -> dict:
    """Save a budget limit for a category and month (YYYY-MM)."""
    row = {
        "user_phone": user_phone,
        "category": category,
        "amount": amount,
        "mes_referencia": mes_referencia,
    }
    result = get_db().table("finbot_budgets").insert(row).execute()
    return result.data[0] if result.data else {}


def get_budget(user_phone: str, category: str, mes_referencia: str) -> float | None:
    """Get the most recent budget for a category up to the given month."""
    result = get_db().rpc(
        "budget_get",
        {"p_phone": user_phone, "p_category": category, "p_mes": mes_referencia},
    ).execute()
    return float(result.data) if result.data else None


def get_all_budgets(user_phone: str, mes_referencia: str) -> list[dict]:
    """Get all category budgets effective for the given month."""
    result = get_db().rpc(
        "budget_all",
        {"p_phone": user_phone, "p_mes": mes_referencia},
    ).execute()
    return result.data or []


def get_budget_history(user_phone: str, category: str) -> list[dict]:
    """Get budget history for a category (last 12 months)."""
    result = get_db().rpc(
        "budget_history",
        {"p_phone": user_phone, "p_category": category},
    ).execute()
    return result.data or []

# ── Conversation ──────────────────────────────────────────────────────────────

def save_message(user_phone: str, role: str, content: str) -> None:
    get_db().table("finbot_conversation").insert({
        "user_phone": user_phone,
        "role": role,
        "content": content,
    }).execute()


def get_history(user_phone: str, limit: int = 12) -> list[dict]:
    """Last N messages in chronological order, ready for LLM context."""
    result = (
        get_db()
        .table("finbot_conversation")
        .select("role, content")
        .eq("user_phone", user_phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    # Reverse so oldest is first (chronological for LLM)
    return list(reversed(result.data or []))


def clear_history(user_phone: str) -> None:
    get_db().table("finbot_conversation").delete().eq("user_phone", user_phone).execute()



# database.py

# ── Funções para Pluggy & Comportamento ───────────────────────────────────────

def get_user_item_id(p_phone: str) -> str | None:
    """Busca o pluggy_item_id vinculado ao telefone do utilizador."""
    res = get_db().table("finbot_user_connections").select("pluggy_item_id").eq("user_phone", p_phone).execute()
    if res.data:
        return res.data[0]["pluggy_item_id"]
    return None

def get_all_user_connections() -> list[dict]:
    """Busca todas as conexões de usuários para sincronização automática."""
    res = (
        get_db().table("finbot_user_connections").select("user_phone").execute()
    )
    return res.data or []

def registrar_gasto_pluggy(user_phone: str, valor: float, categoria: str, descricao: str, pluggy_id: str, tipo: str = "expense", data_tx: str | None = None) -> bool:
    """
    Tenta registar uma transação vindo da Pluggy.
    Retorna True se for um gasto novo, False se já existir (evita duplicados).
    """
    try:
        data = {
            "user_phone": user_phone,
            "amount": valor,
            "category": categoria,
            "description": descricao,
            "pluggy_transaction_id": pluggy_id, # Coluna que adicionámos via SQL
            "transaction_type": tipo,
            "payment_method": "debito"
        }
        if data_tx:
            # Usa a data real da transação (Pluggy envia ISO format)
            data["created_at"] = data_tx

        # Usamos insert simples. Se o pluggy_transaction_id já existir, 
        # a restrição UNIQUE no banco lançará um erro, retornando False.
        get_db().table("finbot_expenses").insert(data).execute()
        return True
    except Exception as e:
        error_msg = str(e).lower()
        # Se for erro de duplicidade (UNIQUE constraint), apenas ignoramos
        if "duplicate" in error_msg or "already exists" in error_msg:
            return False
        # Se for outro erro (coluna inexistente, etc), logamos para debug
        logger.error(f"Erro inesperado ao registrar gasto Pluggy: {e}")
        raise e

def get_budget_limit(p_phone: str, p_category: str) -> float | None:
    """Busca o limite (coluna amount) na sua tabela finbot_budgets."""
    from datetime import date
    mes_atual = date.today().strftime("%Y-%m")
    
    res = get_db().table("finbot_budgets") \
        .select("amount") \
        .eq("user_phone", p_phone) \
        .ilike("category", p_category) \
        .eq("mes_referencia", mes_atual) \
        .execute()
    
    if res.data:
        return float(res.data[0]["amount"])
    return None