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


# ── Expenses ──────────────────────────────────────────────────────────────────

def save_expense(user_phone: str, amount: float, category: str, description: str) -> dict:
    row = {
        "user_phone": user_phone,
        "amount": amount,
        "category": category,
        "description": description,
    }
    result = get_db().table("finbot_expenses").insert(row).execute()
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
