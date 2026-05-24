# app/database.py
# ─────────────────────────────────────────────────────────────────────────────
# Camada de Persistência Supabase — Versão Unificada de Produção
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from supabase import create_client, Client
from app.config import get_settings

logger = logging.getLogger(__name__)

@lru_cache
def get_db() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)

# ── User Settings & Onboarding ───────────────────────────────────────────────

def get_user_settings(user_phone: str) -> dict | None:
    try:
        result = get_db().table("finbot_user_settings").select("*").eq("user_phone", user_phone).limit(1).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error(f"Erro ao buscar configurações de {user_phone}: {exc}")
        return None

def save_user_settings(user_phone: str, dia_vencimento: int, dia_corte: int) -> dict:
    try:
        row = {
            "user_phone": user_phone,
            "cartao_dia_vencimento": dia_vencimento,
            "cartao_dia_corte": dia_corte,
        }
        result = get_db().table("finbot_user_settings").upsert(row, on_conflict="user_phone").execute()
        return result.data[0] if result.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar configurações de {user_phone}: {exc}")
        return {}

def is_new_user(user_phone: str) -> bool:
    return get_user_settings(user_phone) is None

def get_card_settings(user_phone: str) -> tuple[int, int]:
    settings = get_user_settings(user_phone)
    if settings:
        return int(settings["cartao_dia_corte"]), int(settings["cartao_dia_vencimento"])
    s = get_settings()
    return (int(s.cartao_dia_corte) if s.cartao_dia_corte else 24, int(s.cartao_dia_vencimento) if s.cartao_dia_vencimento else 1)

# ── Histórico de Conversas (WhatsApp) ─────────────────────────────────────────

def get_history(user_phone: str, limit: int = 20) -> list[dict]:
    try:
        result = get_db().table("finbot_conversation").select("role, content").eq("user_phone", user_phone).order("created_at", desc=True).limit(limit).execute()
        return list(reversed(result.data or []))
    except Exception as exc:
        logger.error(f"Falha ao carregar histórico: {exc}")
        return []

def save_message(user_phone: str, role: str, content: str) -> None:
    try:
        get_db().table("finbot_conversation").insert({"user_phone": user_phone, "role": role, "content": content}).execute()
    except Exception as exc:
        logger.error(f"Falha ao salvar mensagem: {exc}")

# ── Gerenciamento de Conexões de Usuário (PDF, Pluggy) ────────────────────────
def _get_or_create_user_connection(p_phone: str) -> dict:
    """
    Garante que uma entrada em finbot_user_connections exista para o user_phone.
    Se não existir, cria uma com valores padrão.
    """
    try:
        res = get_db().table("finbot_user_connections").select("*").eq("user_phone", p_phone).limit(1).execute()
        if res.data:
            return res.data[0]
        else:
            # Enviamos "" em vez de None para pluggy_item_id para evitar erro de constraint NOT NULL
            new_data = {"user_phone": p_phone, "pluggy_item_id": "", "status": "ativo"}
            insert_res = get_db().table("finbot_user_connections").insert(new_data).execute()
            return insert_res.data[0] if insert_res.data else new_data
    except Exception as exc:
        logger.error(f"Erro ao obter ou criar conexão para {p_phone}: {exc}")
        raise

def get_user_item_id(user_phone: str) -> str | None:
    try:
        res = get_db().table("finbot_user_connections").select("pluggy_item_id").eq("user_phone", user_phone).execute()
        if res.data and res.data[0].get("pluggy_item_id"):
            return res.data[0]["pluggy_item_id"]
    except Exception:
        pass
    return None

# ── Persistência de Transações (Alinhado 100% com suas RPCs Originais) ────────

def save_expense(user_phone: str, amount: float, category: str, description: str, beneficiario: str | None = None, subcategoria: str | None = None, expense_date: date | None = None, transaction_type: str = "expense", payment_method: str = "debito") -> dict:
    try:
        dt = expense_date or date.today()
        row_data = {
            "user_phone": user_phone,
            "amount": amount,
            "category": category,
            "subcategory": subcategoria,
            "description": description,
            "beneficiario": beneficiario,
            "payment_method": payment_method,
            "transaction_type": transaction_type,
            "created_at": datetime(dt.year, dt.month, dt.day).isoformat()
        }
        result = get_db().table("finbot_expenses").insert(row_data).execute()
        return result.data[0] if result.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar despesa: {exc}")
        raise exc

def save_budget(user_phone: str, category: str, amount: float, mes_referencia: str) -> dict:
    try:
        row = {
            "user_phone": user_phone,
            "category": category,
            "amount": amount,
            "mes_referencia": mes_referencia
        }
        result = get_db().table("finbot_budgets").upsert(row, on_conflict="user_phone, category, mes_referencia").execute()
        return result.data[0] if result.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar orçamento: {exc}")
        return {}

def save_expense_credit(user_phone: str, amount: float, category: str, description: str, beneficiario: str | None = None, subcategoria: str | None = None, expense_date: date | None = None, installment_of: int | None = None, installment_total: int | None = None) -> dict:
    try:
        dt = expense_date or date.today()
        row_data = {
            "user_phone": user_phone,
            "amount": amount,
            "category": category,
            "subcategory": subcategoria,
            "description": description,
            "beneficiario": beneficiario,
            "payment_method": "credito",
            "transaction_type": "expense",
            "installment_of": installment_of,
            "installment_total": installment_total,
            "created_at": datetime(dt.year, dt.month, dt.day).isoformat()
        }
        result = get_db().table("finbot_expenses").insert(row_data).execute()
        return result.data[0] if result.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar despesa de crédito: {exc}")
        raise exc

def _parse_rpc_float(data: Any) -> float:
    """Extrai valor numérico de respostas RPC do Supabase de forma robusta."""
    if isinstance(data, list):
        # Se for uma lista de objetos com coluna 'total' ou lista simples
        if not data: return 0.0
        item = data[0]
        if isinstance(item, dict): return float(item.get("total") or item.get("amount") or 0.0)
        return float(item or 0.0)
    if isinstance(data, dict):
        return float(data.get("total") or data.get("amount") or 0.0)
    return float(data or 0.0)

def monthly_by_category(user_phone: str) -> list[dict]:
    result = get_db().rpc("expenses_by_category", {"p_phone": user_phone}).execute()
    return result.data or []

def monthly_total(user_phone: str) -> float:
    result = get_db().rpc("expenses_monthly_total", {"p_phone": user_phone}).execute()
    return _parse_rpc_float(result.data)

def get_latest_expenses(user_phone: str, limit: int = 10, payment_method: str | None = None) -> list[dict]:
    """Busca os últimos gastos registrados na tabela de despesas."""
    try:
        query = get_db().table("finbot_expenses").select("*").eq("user_phone", user_phone)
        if payment_method:
            query = query.eq("payment_method", payment_method)
        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:
        logger.error(f"Erro ao buscar últimos gastos: {exc}")
        return []

def monthly_income_total(user_phone: str) -> float:
    result = get_db().rpc("income_monthly_total", {"p_phone": user_phone}).execute()
    return _parse_rpc_float(result.data)

def category_total(user_phone: str, category: str) -> float:
    result = get_db().rpc("expenses_category_total", {"p_phone": user_phone, "p_category": category}).execute()
    return _parse_rpc_float(result.data)

def get_budget_history(p_phone: str, p_category: str) -> list[dict]:
    result = get_db().rpc("budget_history", {"p_phone": p_phone, "p_category": p_category}).execute()
    return result.data or []

def expenses_by_fatura(user_phone: str, due_date: str, dia_corte: int) -> list[dict]:
    result = get_db().rpc("expenses_by_fatura", {"p_phone": user_phone, "p_due_date": due_date, "p_corte_day": dia_corte}).execute()
    return result.data or []

def fatura_total(user_phone: str, due_date: str, dia_corte: int) -> float:
    gastos = expenses_by_fatura(user_phone, due_date, dia_corte)
    return round(sum(float(g["amount"]) for g in gastos), 2)

def get_budget_limit(p_phone: str, p_category: str) -> float | None:
    try:
        mes_atual = date.today().strftime("%Y-%m")
        res = get_db().table("finbot_budgets").select("amount").eq("user_phone", p_phone).ilike("category", p_category).eq("mes_referencia", mes_atual).execute()
        if res.data and res.data[0].get("amount") is not None:
            return float(res.data[0]["amount"])
    except Exception:
        return None
    return None

def get_all_budgets(user_phone: str, mes_referencia: str) -> list[dict]:
    result = get_db().rpc("budget_all", {"p_phone": user_phone, "p_mes": mes_referencia}).execute()
    return result.data or []

# ── Fluxo União Estável: Processamento de Extratos e PDFs Pendentes ───────────

def salvar_pdf_aguardando_senha(p_phone: str, media_url: str, status: str = "aguardando_senha"): # type: ignore
    try:
        # Removida verificação redundante; garantida pelo webhook no app/main.py
        data = {"pending_pdf_url": media_url, "status": status}
        get_db().table("finbot_user_connections").update(data).eq("user_phone", p_phone).execute()
    except Exception as exc:
        logger.error(f"Erro crítico ao salvar estado do PDF para {p_phone}: {exc}")

def obter_pdf_pendente(p_phone: str) -> str | None:
    try:
        res = get_db().table("finbot_user_connections").select("pending_pdf_url").eq("user_phone", p_phone).in_("status", ["aguardando_senha", "processando"]).execute()
        if res.data and res.data[0].get("pending_pdf_url"):
            return res.data[0]["pending_pdf_url"]
    except Exception as exc:
        logger.error(f"Erro ao obter PDF pendente para {p_phone}: {exc}")
    return None

def limpar_pdf_pendente(p_phone: str):
    try:
        # Removida verificação redundante; garantida pelo webhook no app/main.py
        data = {"pending_pdf_url": None, "status": "ativo"}
        get_db().table("finbot_user_connections").update(data).eq("user_phone", p_phone).execute()
    except Exception as exc:
        logger.error(f"Erro crítico ao limpar PDF pendente para {p_phone}: {exc}")

def filtrar_transacoes_existentes(user_phone: str, tx_ids: list[str]) -> set[str]:
    """
    Recebe uma lista de IDs e retorna quais deles já existem no banco.
    Evita o problema de N+1 consultas de duplicidade.
    """
    if not tx_ids:
        return set()
    try:
        res = get_db().table("finbot_expenses").select("pluggy_transaction_id").eq("user_phone", user_phone).in_("pluggy_transaction_id", tx_ids).execute()
        return {row["pluggy_transaction_id"] for row in res.data}
    except Exception as exc:
        logger.error(f"Erro ao filtrar transações existentes: {exc}")
        return set()

def inserir_gastos_em_lote(gastos: list[dict]) -> bool:
    """
    Realiza um upsert múltiplo no Supabase. 
    Garante que duplicatas não quebrem o lote.
    """
    if not gastos:
        return True
    try:
        # Usamos upsert com on_conflict para garantir que se um ID já existir, ele apenas ignore ou atualize
        get_db().table("finbot_expenses").upsert(gastos, on_conflict="pluggy_transaction_id").execute()
        return True
    except Exception as exc:
        logger.error(f"Falha no insert em lote: {exc}")
        return False

def registrar_gasto_automatico(user_phone: str, valor: float, category: str, description: str, tx_id: str, metodo: str = "debito", tipo: str = "expense", data_tx: str | None = None) -> bool:
    try:
        # Evita duplicidade pelo ID da Pluggy ou ID gerado do PDF
        existe = get_db().table("finbot_expenses").select("id").eq("pluggy_transaction_id", tx_id).execute()
        if existe.data: return False
        
        data = {
            "user_phone": user_phone, "amount": abs(valor), "category": category, 
            "description": description, "transaction_type": tipo, 
            "payment_method": metodo, "pluggy_transaction_id": tx_id
        }
        if data_tx: data["created_at"] = data_tx
            
        get_db().table("finbot_expenses").insert(data).execute()
        return True
    except Exception as exc:
        logger.error(f"Falha ao registrar transação automática {tx_id}: {exc}")
        return False

def registrar_gasto_pluggy(user_phone: str, valor: float, categoria: str, descricao: str, pluggy_id: str, tipo: str, data_tx: str) -> bool:
    """Wrapper para manter compatibilidade com a assinatura do pluggy_service.py"""
    return registrar_gasto_automatico(user_phone, valor, categoria, descricao, pluggy_id, tipo=tipo, data_tx=data_tx)