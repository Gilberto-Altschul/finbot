"""Itens (bancos conectados) que pertencem a cada usuário."""
from __future__ import annotations

import logging

import app.database as db
from app.utils import get_lookup_prefix

logger = logging.getLogger(__name__)
TABELA = "finbot_pluggy_itens"


def ler_ids(user_phone: str) -> list[str]:
    try:
        res = db.get_db().table(TABELA).select("item_id").eq("user_key", get_lookup_prefix(user_phone)).execute()
        return [r["item_id"] for r in (res.data or []) if r.get("item_id")]
    except Exception as e:
        logger.warning(f"Não foi possível ler os itens do usuário: {e}")
        return []


def registrar(user_phone: str, item_id: str, banco_codigo: str | None = None, banco_nome: str | None = None) -> None:
    """Grava o vínculo usuário -> item (idempotente)."""
    linha = {"user_key": get_lookup_prefix(user_phone), "item_id": item_id}
    if banco_codigo:
        linha["banco_codigo"] = banco_codigo
    if banco_nome:
        linha["banco_nome"] = banco_nome
    try:
        db.get_db().table(TABELA).upsert(linha, on_conflict="user_key,item_id").execute()
    except Exception as e:
        logger.warning(f"Não foi possível registrar o item {item_id}: {e}")
