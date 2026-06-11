# app/database.py
# ─────────────────────────────────────────────────────────────────────────────
# Camada de Persistência Supabase — Versão Unificada de Produção
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, time
from functools import lru_cache
from typing import Any
from calendar import monthrange

from supabase import create_client, Client
from app.config import get_settings
from app.utils import _normalize, criptografar_telefone, get_lookup_prefix

logger = logging.getLogger(__name__)

@lru_cache
def get_db() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_key)

# Helpers internos para facilitar a transição para dados criptografados
def _q(phone: str) -> str: return f"{get_lookup_prefix(phone)}:%"
def _s(phone: str) -> str: return criptografar_telefone(phone)

# ── User Settings & Onboarding ───────────────────────────────────────────────

def _get_or_create_user_connection(user_phone: str) -> dict:
    """Garante que o usuário tenha uma entrada na tabela de conexões."""
    try:
        res = get_db().table("finbot_user_connections").select("*").ilike("user_phone", _q(user_phone)).limit(1).execute()
        if res.data:
            return res.data[0]
        
        row = {"user_phone": _s(user_phone), "status": "ativo"}
        res = get_db().table("finbot_user_connections").insert(row).execute()
        return res.data[0] if res.data else {}
    except Exception as e:
        logger.error(f"Erro em _get_or_create_user_connection: {e}")
        return {}

def get_user_settings(user_phone: str) -> dict | None:
    try:
        result = get_db().table("finbot_user_connections").select("*").ilike("user_phone", _q(user_phone)).limit(1).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error(f"Erro ao buscar configurações de {user_phone}: {exc}")
        return None

def save_user_settings(user_phone: str, dia_vencimento: int, dia_corte: int) -> dict:
    try:
        _get_or_create_user_connection(user_phone)
        row = {
            "user_phone": _s(user_phone),
            "cartao_dia_vencimento": dia_vencimento,
            "cartao_dia_corte": dia_corte
        }
        result = get_db().table("finbot_user_connections").upsert(row, on_conflict="user_phone").execute()
        return result.data[0]
    except Exception as exc:
        logger.error(f"Erro ao salvar configurações de {user_phone}: {exc}")
        raise exc

def is_new_user(user_phone: str) -> bool:
    return get_user_settings(user_phone) is None

def get_card_settings(user_phone: str) -> tuple[int, int]:
    cfg = get_user_settings(user_phone)
    if not cfg:
        return 5, 12
    return int(cfg.get("cartao_dia_corte", 5)), int(cfg.get("cartao_dia_vencimento", 12))

# ── Chat History ──────────────────────────────────────────────────────────────

def get_history(user_phone: str, limit: int = 12) -> list[dict]:
    try:
        res = get_db().table("finbot_conversation") \
            .select("role, content") \
            .ilike("user_phone", _q(user_phone)) \
            .order("created_at", desc=True) \
            .limit(limit).execute()
        
        records = res.data or []
        records.reverse()
        return records
    except Exception as exc:
        logger.error(f"Erro ao carregar histórico de {user_phone}: {exc}")
        return []

def save_message(user_phone: str, role: str, content: str) -> None:
    try:
        row = {"user_phone": _s(user_phone), "role": role, "content": content}
        get_db().table("finbot_conversation").insert(row).execute()
    except Exception as exc:
        logger.error(f"Erro ao salvar mensagem: {exc}")

# ── Core Financial Transactions (Manual) ──────────────────────────────────────

def save_expense(user_phone: str, valor: float, category: str, description: str, beneficiario: str | None = None, subcategoria: str | None = None, expense_date: date | None = None, transaction_type: str = "expense", payment_method: str = "debito") -> dict:
    try:
        _get_or_create_user_connection(user_phone)
        dt = expense_date or date.today()
        row = {
            "user_phone": _s(user_phone),
            "amount": abs(valor),
            "category": category,
            "subcategory": subcategoria or "Outros",
            "description": description,
            "beneficiario": beneficiario,
            "transaction_type": transaction_type,
            "payment_method": payment_method,
            "purchase_date": dt.isoformat(),
            "billing_date": dt.isoformat()
        }
        res = get_db().table("finbot_expenses").insert(row).execute()
        return res.data[0] if res.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar transação manual: {exc}")
        raise exc

def save_expense_credit(user_phone: str, valor: float, category: str, description: str, beneficiario: str | None = None, subcategoria: str | None = None, fatura_date: date | None = None, p_atual: int | None = None, p_total: int | None = None, purchase_date: date | None = None) -> dict:
    try:
        _get_or_create_user_connection(user_phone)
        dt_fat = fatura_date or date.today()
        row = {
            "user_phone": _s(user_phone),
            "amount": abs(valor),
            "category": category,
            "subcategory": subcategoria or "Outros",
            "description": description,
            "beneficiario": beneficiario,
            "transaction_type": "expense",
            "payment_method": "credito",
            "installment_of": p_atual,
            "installment_total": p_total,
            "purchase_date": (purchase_date or date.today()).isoformat(),
            "billing_date": dt_fat.isoformat()
        }
        res = get_db().table("finbot_expenses").insert(row).execute()
        return res.data[0] if res.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar transação de crédito: {exc}")
        raise exc

# ── Financial Math & Calculations (IGNORANDO HORAS COMPLETAMENTE) ───────────────────────────

def monthly_total(user_phone: str, mes_ref: str | None = None, dia_inicio: int = 1, dia_fim: int | None = None) -> float:
    try:
        ref = datetime.strptime(mes_ref, "%Y-%m").date() if mes_ref else date.today()
        start = ref.replace(day=max(1, dia_inicio)).isoformat()
        
        max_dia = monthrange(ref.year, ref.month)[1]
        fim_val = min(dia_fim, max_dia) if dia_fim else max_dia
        end = ref.replace(day=fim_val).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("amount") \
            .ilike("user_phone", _q(user_phone)) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .gte("billing_date", start) \
            .lte("billing_date", end).execute()
        return round(sum(float(item["amount"]) for item in (res.data or [])), 2)
    except Exception as exc:
        logger.error(f"Erro cálculo monthly_total: {exc}")
        return 0.0

def monthly_income_total(user_phone: str, mes_ref: str | None = None, dia_inicio: int = 1, dia_fim: int | None = None) -> float:
    try:
        ref = datetime.strptime(mes_ref, "%Y-%m").date() if mes_ref else date.today()
        start = ref.replace(day=max(1, dia_inicio)).isoformat()
        
        max_dia = monthrange(ref.year, ref.month)[1]
        fim_val = min(dia_fim, max_dia) if dia_fim else max_dia
        end = ref.replace(day=fim_val).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("amount") \
            .ilike("user_phone", _q(user_phone)) \
            .ilike("transaction_type", "income") \
            .gte("billing_date", start) \
            .lte("billing_date", end).execute()
        return round(sum(float(item["amount"]) for item in (res.data or [])), 2)
    except Exception as exc:
        logger.error(f"Erro cálculo income total: {exc}")
        return 0.0

def category_total(user_phone: str, category: str, mes_ref: str | None = None) -> float:
    try:
        ref = datetime.strptime(mes_ref, "%Y-%m").date() if mes_ref else date.today()
        start = ref.replace(day=1).isoformat()
        ultimo_dia = monthrange(ref.year, ref.month)[1]
        end = ref.replace(day=ultimo_dia).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("amount") \
            .ilike("user_phone", _q(user_phone)) \
            .ilike("category", category.strip()) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .gte("billing_date", start) \
            .lte("billing_date", end).execute()
        return round(sum(float(item["amount"]) for item in (res.data or [])), 2)
    except Exception as exc:
        logger.error(f"Erro cálculo category_total {category}: {exc}")
        return 0.0

def monthly_by_category(user_phone: str, mes_ref: str | None = None, dia_inicio: int = 1, dia_fim: int | None = None) -> list[dict]:
    try:
        ref = datetime.strptime(mes_ref, "%Y-%m").date() if mes_ref else date.today()
        start = ref.replace(day=max(1, dia_inicio)).isoformat()
        
        max_dia = monthrange(ref.year, ref.month)[1]
        fim_val = min(dia_fim, max_dia) if dia_fim else max_dia
        end = ref.replace(day=fim_val).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("category, amount") \
            .ilike("user_phone", _q(user_phone)) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .gte("billing_date", start) \
            .lte("billing_date", end).execute()
        
        data = res.data or []
        agrupado = {}
        for item in data:
            raw_cat = item["category"].strip() if item["category"] else "Outros"
            # Agrupamento robusto usando normalização para evitar duplicidade por acentos/caixa
            key = next((k for k in agrupado if _normalize(k) == _normalize(raw_cat)), raw_cat)
            agrupado[key] = agrupado.get(key, 0.0) + float(item["amount"])
        
        return [{"category": k, "total": round(v, 2)} for k, v in agrupado.items()]
    except Exception as exc:
        logger.error(f"Erro agrupamento categorias: {exc}")
        return []

def fatura_total(user_phone: str, fatura_date_iso: str, dia_corte: int) -> float:
    try:
        tgt = datetime.strptime(fatura_date_iso[:10], "%Y-%m-%d").date()
        start = tgt.replace(day=1).isoformat()
        ultimo_dia = monthrange(tgt.year, tgt.month)[1]
        end = tgt.replace(day=ultimo_dia).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("amount, transaction_type") \
            .ilike("user_phone", _q(user_phone)) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .eq("payment_method", "credito") \
            .gte("billing_date", start) \
            .lte("billing_date", end).execute()
        
        total = sum(float(item["amount"]) if item["transaction_type"] == "expense" else -float(item["amount"]) 
                    for item in (res.data or []))
        return round(total, 2)
    except Exception as exc:
        logger.error(f"Erro cálculo fatura_total: {exc}")
        return 0.0

def expenses_by_fatura(user_phone: str, fatura_date_iso: str, dia_corte: int) -> list[dict]:
    try:
        tgt = datetime.strptime(fatura_date_iso[:10], "%Y-%m-%d").date()
        start = tgt.replace(day=1).isoformat()
        ultimo_dia = monthrange(tgt.year, tgt.month)[1]
        end = tgt.replace(day=ultimo_dia).isoformat()

        res = get_db().table("finbot_expenses") \
            .select("description, amount, billing_date, purchase_date, installment_of, installment_total") \
            .ilike("user_phone", _q(user_phone)) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .eq("payment_method", "credito") \
            .gte("billing_date", start) \
            .lte("billing_date", end) \
            .order("purchase_date", desc=True).execute()
        return res.data or []
    except Exception as exc:
        logger.error(f"Erro buscando despesas da fatura: {exc}")
        return []

# ── Budgeting & Limits ────────────────────────────────────────────────────────

def save_budget(user_phone: str, category: str, amount: float, mes_ref: str) -> dict:
    try:
        _get_or_create_user_connection(user_phone)

        # Como o user_phone criptografado é não-determinístico, usamos o 
        # prefixo HMAC (_q) para verificar se já existe um orçamento.
        check = get_db().table("finbot_budgets").select("id") \
            .ilike("user_phone", _q(user_phone)) \
            .eq("category", category) \
            .eq("mes_referencia", mes_ref).execute()

        row = {"user_phone": _s(user_phone), "category": category, "amount": amount, "mes_referencia": mes_ref}
        
        if check.data:
            # Atualiza o registro existente pelo ID primário
            res = get_db().table("finbot_budgets").update(row).eq("id", check.data[0]["id"]).execute()
        else:
            res = get_db().table("finbot_budgets").insert(row).execute()
            
        return res.data[0] if res.data else {}
    except Exception as exc:
        logger.error(f"Erro ao salvar limite: {exc}")
        raise exc

def get_budget(user_phone: str, category: str, mes_ref: str) -> float | None:
    try:
        # Busca o limite mais recente que seja menor ou igual ao mês solicitado
        res = get_db().table("finbot_budgets") \
            .select("amount") \
            .ilike("user_phone", _q(user_phone)) \
            .eq("category", category) \
            .lte("mes_referencia", mes_ref) \
            .order("mes_referencia", desc=True) \
            .limit(1).execute()
        return float(res.data[0]["amount"]) if res.data else None
    except Exception as e:
        logger.error(f"Erro ao obter orçamento: {e}")
        return None

def get_all_budgets(user_phone: str, mes_ref: str) -> list[dict]:
    try:
        # Busca todos os limites históricos até o mês de referência
        res = get_db().table("finbot_budgets").select("category, amount, mes_referencia") \
            .ilike("user_phone", _q(user_phone)) \
            .lte("mes_referencia", mes_ref) \
            .order("mes_referencia", desc=False).execute()
        
        # Consolida para manter apenas o valor mais recente de cada categoria (case-insensitive)
        budgets = {} # { "norm_cat": {"category": "Nome", "amount": 0.0} }
        for item in (res.data or []):
            cat_name = item["category"]
            norm = _normalize(cat_name)
            budgets[norm] = {"category": cat_name, "amount": float(item["amount"])}
            
        return list(budgets.values())
    except Exception as e:
        logger.error(f"Erro ao obter todos os orçamentos: {e}")
        return []

def get_top_maiores_gastos(user_phone: str, limit: int = 5) -> list[dict]:
    try:
        hoje = date.today()
        start = hoje.replace(day=1).isoformat()
        ultimo_dia = monthrange(hoje.year, hoje.month)[1]
        end = hoje.replace(day=ultimo_dia).isoformat()

        res = get_db().table("finbot_expenses").select("description, amount, category") \
            .ilike("user_phone", _q(user_phone)) \
            .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
            .gte("billing_date", start) \
            .lte("billing_date", end) \
            .order("amount", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao obter maiores gastos: {e}")
        return []

def get_daily_trend(user_phone: str, days: int = 7) -> list[dict]:
    try:
        res = get_db().rpc("expenses_daily_trend", {"p_phone": _s(user_phone), "p_days": days}).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao buscar tendência diária: {e}")
        return []

def get_resumo_mes_anterior(user_phone: str) -> dict:
    try:
        hoje = date.today()
        primeiro_dia_atual = hoje.replace(day=1)
        fim_mes_passado = primeiro_dia_atual - timedelta(days=1)
        inicio_mes_passado = fim_mes_passado.replace(day=1)
        
        start = inicio_mes_passado.isoformat()
        end = fim_mes_passado.isoformat()
        
        res_exp = get_db().table("finbot_expenses").select("amount") \
            .ilike("user_phone", _q(user_phone)).or_("transaction_type.is.null,transaction_type.not.ilike.income").gte("billing_date", start).lte("billing_date", end).execute()
        res_inc = get_db().table("finbot_expenses").select("amount") \
            .ilike("user_phone", _q(user_phone)).ilike("transaction_type", "income").gte("billing_date", start).lte("billing_date", end).execute()
            
        g = sum(float(x["amount"]) for x in (res_exp.data or []))
        r = sum(float(x["amount"]) for x in (res_inc.data or []))
        return {"gastos": g, "receitas": r, "saldo": round(r - g, 2)}
    except Exception as e:
        logger.error(f"Erro no resumo do mês anterior: {e}")
        return {"gastos": 0.0, "receitas": 0.0, "saldo": 0.0}

def get_active_installments(user_phone: str) -> list[dict]:
    try:
        res = get_db().table("finbot_expenses").select("description, amount, installment_of, installment_total") \
            .ilike("user_phone", _q(user_phone)) \
            .not_.is_("installment_of", "null") \
            .order("purchase_date", desc=True).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao buscar parcelamentos ativos: {e}")
        return []

def get_expenses_by_category_current_month(user_phone: str, category: str) -> list[dict]:
    try:
        hoje = date.today()
        start = hoje.replace(day=1).isoformat()
        ultimo_dia = monthrange(hoje.year, hoje.month)[1]
        end = hoje.replace(day=ultimo_dia).isoformat()

        res = get_db().table("finbot_expenses").select("subcategory, description, amount") \
            .ilike("user_phone", _q(user_phone)) \
            .ilike("category", category) \
            .not_.ilike("transaction_type", "income") \
            .gte("billing_date", start).lte("billing_date", end).execute()
        return res.data or []
    except Exception as e:
        logger.error(f"Erro ao buscar gastos por category: {e}")
        return []

def get_category_emojis() -> dict[str, str]:
    try:
        res = get_db().table("finbot_categories").select("name, emoji").execute()
        return {item["name"]: item["emoji"] for item in (res.data or [])}
    except Exception as e:
        logger.error(f"Erro ao buscar emojis de categoria: {e}")
        return {}

def get_user_merchant_mapping(user_phone: str, merchant: str) -> dict | None:
    """Busca se já existe um mapeamento de categoria/subcategoria para este estabelecimento."""
    try:
        m_norm = _normalize(merchant)
        res = get_db().table("finbot_merchant_mappings") \
            .select("category, subcategory") \
            .ilike("user_phone", _q(user_phone)) \
            .eq("merchant_name", m_norm) \
            .limit(1).execute()
        
        if res.data:
            return {
                "category_name": res.data[0]["category"],
                "subcategory_name": res.data[0]["subcategory"]
            }
        return None
    except Exception as e:
        logger.error(f"Erro em get_user_merchant_mapping: {e}")
        return None

def save_user_merchant_mapping(user_phone: str, merchant: str, category: str, subcategory: str) -> None:
    try:
        m_norm = _normalize(merchant)
        row = {"user_phone": _s(user_phone), "merchant_name": m_norm, "category": category, "subcategory": subcategory}
        get_db().table("finbot_merchant_mappings").upsert(row, on_conflict="user_phone,merchant_name").execute()
    except Exception as e:
        logger.error(f"Erro ao salvar mapeamento do estabelecimento: {e}")

def obter_pdf_pendente(user_phone: str) -> tuple[str, str] | None:
    """Busca se o usuário possui algum PDF pendente de processamento na tabela de conexões, retornando a URL e o status."""
    try:
        res = get_db().table("finbot_user_connections") \
            .select("pending_pdf_url, status") \
            .ilike("user_phone", _q(user_phone)) \
            .in_("status", ["aguardando_senha", "processando"]) \
            .limit(1).execute()
        
        if res.data and res.data[0].get("pending_pdf_url"):
            return res.data[0]["pending_pdf_url"], res.data[0]["status"]
        return None
    except Exception as e:
        logger.error(f"Erro em obter_pdf_pendente para {user_phone}: {e}")
        return None

def salvar_pdf_aguardando_senha(user_phone: str, media_url: str, status: str = "aguardando_senha") -> None:
    """Registra que o usuário enviou um PDF que precisa de processamento ou senha."""
    try:
        row = {
            "user_phone": _s(user_phone),
            "pending_pdf_url": media_url,
            "status": status
        }
        get_db().table("finbot_user_connections").upsert(row, on_conflict="user_phone").execute()
    except Exception as e:
        logger.error(f"Erro em salvar_pdf_aguardando_senha: {e}")

def limpar_pdf_pendente(user_phone: str) -> None:
    """Limpa o estado de processamento de PDF do usuário após a conclusão ou erro."""
    try:
        row = {
            "user_phone": _s(user_phone),
            "pending_pdf_url": None,
            "status": "ativo"
        }
        get_db().table("finbot_user_connections").upsert(row, on_conflict="user_phone").execute()
    except Exception as e:
        logger.error(f"Erro em limpar_pdf_pendente: {e}")

def filtrar_transacoes_existentes(user_phone: str, tx_ids: list[str]) -> set[str]:
    """Retorna um conjunto de IDs que já existem no banco para evitar duplicatas em lote."""
    if not tx_ids:
        return set()

    try:
        existentes = set()
        # PostgREST/Supabase limitam o tamanho da URL. Batcheamos em 50 IDs por vez 
        # para garantir que a requisição não seja rejeitada (Erro 400).
        batch_size = 50
        for i in range(0, len(tx_ids), batch_size):
            batch = tx_ids[i:i + batch_size]
            res = get_db().table("finbot_expenses") \
                .select("pluggy_transaction_id") \
                .ilike("user_phone", _q(user_phone)) \
                .in_("pluggy_transaction_id", batch).execute()
            
            if res.data:
                for item in res.data:
                    tid = item.get("pluggy_transaction_id")
                    if tid:
                        existentes.add(str(tid).strip().lower())
        return existentes
    except Exception as e:
        logger.error(f"Erro em filtrar_transacoes_existentes: {e}")
        raise e # Interrompe para evitar importação duplicada por falha de consulta

def inserir_gastos_em_lote(rows: list[dict]) -> bool:
    """Insere várias transações de uma única vez para otimizar a performance."""
    if not rows:
        return True
    for r in rows:
        if "user_phone" in r:
            r["user_phone"] = _s(r["user_phone"])
    try:
        # Batcheamos a inserção em blocos de 100 para evitar payloads JSON excessivos.
        batch_size = 100
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            get_db().table("finbot_expenses").insert(batch).execute()
        return True
    except Exception as e:
        logger.error(f"Erro ao inserir gastos em lote: {e}")
        return False

def get_user_item_id(user_phone: str) -> str | None:
    """Recupera o ID de conexão da Pluggy associado ao usuário."""
    try:
        res = get_db().table("finbot_user_connections").select("pluggy_item_id").ilike("user_phone", _q(user_phone)).limit(1).execute()
        return res.data[0]["pluggy_item_id"] if res.data else None
    except Exception as e:
        logger.error(f"Erro em get_user_item_id: {e}")
        return None

def registrar_gasto_pluggy(user_phone: str, valor: float, categoria: str, descricao: str, pluggy_id: str, tipo: str = "expense", data_tx: str | None = None, payment_method: str = "debito") -> bool:
    """Registra um gasto vindo da Pluggy se o ID ainda não existir."""
    try:
        # Verifica duplicata individual
        res = get_db().table("finbot_expenses").select("id").eq("pluggy_transaction_id", pluggy_id).execute()
        if res.data:
            return False
            
        row = {
            "user_phone": _s(user_phone),
            "amount": valor,
            "category": categoria,
            "description": descricao,
            "pluggy_transaction_id": pluggy_id,
            "transaction_type": tipo,
            "payment_method": payment_method,
            "purchase_date": (data_tx[:10] if data_tx else date.today().isoformat()),
            "billing_date": (data_tx[:10] if data_tx else date.today().isoformat())
        }
        get_db().table("finbot_expenses").insert(row).execute()
        return True
    except Exception as e:
        logger.error(f"Erro em registrar_gasto_pluggy: {e}")
        return False
# Adicionar ao final do app/database.py

def obter_transacoes_paginadas(user_phone: str, mes: int, ano: int, categoria: str = None, pagina: int = 1, tamanho: int = 9, ordem: str = "DESC", dia_inicio: int = 1, dia_fim: int | None = None) -> list[dict]:
    offset = (pagina - 1) * tamanho
    
    inicio_dt = date(ano, mes, max(1, dia_inicio))
    max_dia = monthrange(ano, mes)[1]
    fim_val = min(dia_fim, max_dia) if dia_fim else max_dia
    fim_dt = date(ano, mes, fim_val)
    
    query = get_db().table("finbot_expenses").select("*").ilike("user_phone", _q(user_phone)) \
        .gte("billing_date", inicio_dt.isoformat()).lte("billing_date", fim_dt.isoformat())
    
    if categoria:
        query = query.eq("category", categoria)
        
    is_desc = ordem.upper() == "DESC"
    res = query.order("purchase_date", desc=is_desc).range(offset, offset + tamanho - 1).execute()
    return res.data or []

def atualizar_transacao(tx_id: str, updates: dict):
    return get_db().table("finbot_expenses").update(updates).eq("id", tx_id).execute()

def excluir_transacao(tx_id: str):
    return get_db().table("finbot_expenses").delete().eq("id", tx_id).execute()