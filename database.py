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
#     description text not null
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

def save_expense(user_phone: str, amount: float, category: str, description: str) -> dict:
    row = {
        "user_phone": user_phone,
        "amount": amount,
        "category": category,
        "description": description,
    }
    result = get_db().table("finbot_expenses").insert(row).execute()
    logger.info(f"Supabase insert result for finbot_expenses: {result.data}")
    return result.data[0]


def monthly_by_category(user_phone: str) -> list[dict]:
    """Total per category for the current calendar month."""
    # Supabase doesn't support GROUP BY natively via the client,
    # so we use a raw RPC call to a Postgres function.
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
        .select("amount, category, description, created_at")
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


def save_expense_credit(
    user_phone: str,
    amount: float,
    category: str,
    description: str,
    installment_of: int | None = None,
    installment_total: int | None = None,
) -> dict:
    """Save a credit card expense with optional installment info."""
    row = {
        "user_phone": user_phone,
        "amount": amount,
        "category": category,
        "description": description,
        "payment_method": "credito",
        "installment_of": installment_of,
        "installment_total": installment_total,
    }
    result = get_db().table("finbot_expenses").insert(row).execute()
    return result.data[0]


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
