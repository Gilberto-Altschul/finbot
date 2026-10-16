"""
Dados do banco (saldos e faturas) guardados no Supabase.

Os comandos leem daqui; a Pluggy só é consultada na 1ª vez do dia (ou no "sincronizar").
Motivo: o banco renova os dados poucas vezes por dia — perguntar a cada comando não traz nada
novo, só custa tempo e deixa o bot dependente da Pluggy estar no ar. Se ela cair, o último dado
guardado é mostrado com um aviso.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta

import app.database as db
from app import faturas as fat
from app.utils import get_lookup_prefix

logger = logging.getLogger(__name__)

T_CONTAS, T_HIST, T_FATURAS = "finbot_contas", "finbot_saldo_historico", "finbot_faturas"


# ── tempo ────────────────────────────────────────────────────────────────────
def _fuso():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        return timezone(timedelta(hours=-3))


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dia_local(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(_fuso()).date()
    except ValueError:
        return None


def hoje_local() -> date:
    return datetime.now(_fuso()).date()


# ── leitura ──────────────────────────────────────────────────────────────────
def ler_contas(user_phone: str) -> list[dict]:
    res = db.get_db().table(T_CONTAS).select("*").eq("user_key", get_lookup_prefix(user_phone)).execute()
    return [{"id": r["account_id"], "item_id": r.get("item_id"), "tipo": r["tipo"], "nome": r.get("nome"),
             "saldo": None if r.get("saldo") is None else float(r["saldo"]),
             "atualizado_iso": r.get("atualizado_banco"), "sincronizado_em": r.get("sincronizado_em")}
            for r in (res.data or [])]


def ler_faturas(user_phone: str, account_id: str) -> list[tuple[fat.Ciclo, str]]:
    """[(Ciclo, status)] do mais antigo ao mais novo, a partir do que está guardado."""
    res = (db.get_db().table(T_FATURAS).select("*")
           .eq("user_key", get_lookup_prefix(user_phone)).eq("account_id", account_id).execute())
    hoje = hoje_local()
    itens = []
    for r in sorted(res.data or [], key=lambda r: r["vencimento"]):
        c = fat.Ciclo(date.fromisoformat(str(r["fechamento"])[:10]), date.fromisoformat(str(r["vencimento"])[:10]),
                      None if r.get("total") is None else float(r["total"]), r["bill_id"],
                      fechamento_estimado=bool(r.get("fechamento_estimado")))
        st = "paga" if r.get("paga") else ("a_vencer" if c.vencimento >= hoje else "sem_pagamento")
        itens.append((c, st))
    return itens


def esta_em_dia(contas: list[dict], hoje: date | None = None) -> bool:
    """Em dia = existe dado e TODAS as contas foram buscadas hoje (horário de Brasília)."""
    hoje = hoje or hoje_local()
    return bool(contas) and all(_dia_local(c.get("sincronizado_em")) == hoje for c in contas)


# ── escrita ──────────────────────────────────────────────────────────────────
async def atualizar(pluggy_factory, user_phone: str) -> int:
    """Busca na Pluggy e grava contas, histórico de saldo e faturas. Devolve nº de contas gravadas."""
    pluggy = pluggy_factory()
    contas = await fat.carregar_contas(pluggy, user_phone)
    if not contas:
        raise RuntimeError("A Pluggy não devolveu nenhuma conta.")

    key, agora = get_lookup_prefix(user_phone), _agora_iso()
    anteriores = {r["account_id"]: r for r in (db.get_db().table(T_CONTAS).select("*").eq("user_key", key).execute().data or [])}

    linhas, historico = [], []
    for c in contas:
        linhas.append({"user_key": key, "account_id": c["id"], "item_id": c["item_id"], "tipo": c["tipo"],
                       "nome": c["nome"], "saldo": c["saldo"], "credit_limit": c.get("limite"),
                       "available_limit": c.get("limite_disponivel"),
                       "atualizado_banco": c.get("atualizado_iso"), "sincronizado_em": agora})
        ant = anteriores.get(c["id"])
        if c["saldo"] is not None and (ant is None or ant.get("saldo") is None
                                       or round(float(ant["saldo"]), 2) != round(float(c["saldo"]), 2)):
            historico.append({"user_key": key, "account_id": c["id"], "saldo": c["saldo"], "capturado_em": agora})
    db.get_db().table(T_CONTAS).upsert(linhas, on_conflict="user_key,account_id").execute()
    if historico:
        db.get_db().table(T_HIST).insert(historico).execute()

    for c in (c for c in contas if c["tipo"] == "CREDIT"):
        try:
            bills = await pluggy.listar_faturas(c["id"])
        except Exception as e:  # um cartão sem /bills não pode derrubar o resto
            logger.warning(f"Faturas indisponíveis para {c['id']}: {e}")
            continue
        itens = fat.status_dos_ciclos(bills, hoje_local())
        if itens:
            db.get_db().table(T_FATURAS).upsert(
                [{"user_key": key, "bill_id": ci.bill_id, "account_id": c["id"], "vencimento": ci.vencimento.isoformat(),
                  "fechamento": ci.fechamento.isoformat(), "fechamento_estimado": ci.fechamento_estimado,
                  "total": ci.total, "paga": st == "paga", "sincronizado_em": agora} for ci, st in itens],
                on_conflict="user_key,bill_id").execute()
    return len(linhas)


async def garantir(pluggy_factory, user_phone: str, forcar: bool = False) -> tuple[list[dict], bool]:
    """Devolve (contas, desatualizado). Busca na Pluggy só se o dado não for de hoje (ou forcar)."""
    guardadas = ler_contas(user_phone)
    if not forcar and esta_em_dia(guardadas):
        return guardadas, False
    try:
        await atualizar(pluggy_factory, user_phone)
        return ler_contas(user_phone), False
    except Exception as e:
        logger.warning(f"Atualização pela Pluggy falhou ({e}).")
        if guardadas:
            return guardadas, True
        raise


# ── mensagens ────────────────────────────────────────────────────────────────
def _aviso(contas: list[dict], desatualizado: bool) -> str:
    if not desatualizado:
        return ""
    dia = next((_dia_local(c.get("sincronizado_em")) for c in contas if c.get("sincronizado_em")), None)
    return f"\n\n⚠️ _Não consegui falar com o banco agora; mostrando os dados de {dia.strftime('%d/%m') if dia else 'antes'}._"


async def mensagem_saldos(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    msg = fat.formatar_saldos(contas)
    return (msg + _aviso(contas, velho)) if msg else ""


async def mensagem_faturas(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    cartoes = [c for c in contas if c["tipo"] == "CREDIT"]
    if not cartoes:
        return "💳 Não encontrei nenhum cartão de crédito conectado."
    partes = [fat.formatar_lista_faturas(ler_faturas(user_phone, c["id"]), c["saldo"], c["nome"], hoje_local()) for c in cartoes]
    return "\n\n".join(partes) + _aviso(contas, velho)


async def mensagem_fatura_aberta(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    cartoes = [c for c in contas if c["tipo"] == "CREDIT"]
    if not cartoes:
        return "💳 Não encontrei nenhum cartão de crédito conectado."
    partes = [fat.formatar_aberta([ci for ci, _ in ler_faturas(user_phone, c["id"])], c["saldo"], hoje_local()) for c in cartoes]
    return "\n\n".join(partes) + _aviso(contas, velho)


async def total_do_banco(pluggy_factory, user_phone: str, vencimento: date) -> float | None:
    """Total oficial da fatura que vence no mês de `vencimento` (a partir do que está guardado)."""
    contas, _ = await garantir(pluggy_factory, user_phone)
    for c in (c for c in contas if c["tipo"] == "CREDIT"):
        for ci, _st in ler_faturas(user_phone, c["id"]):
            if (ci.vencimento.year, ci.vencimento.month) == (vencimento.year, vencimento.month):
                return ci.total
    return None
