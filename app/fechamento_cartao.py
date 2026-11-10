"""Ajuste do fechamento de UMA fatura de cartão, informado pelo usuário no WhatsApp.

Quando o banco antecipa/posterga o corte e a Pluggy ainda não trouxe a fatura, o usuário diz, por exemplo:
  "o Itaú fecha dia 20 em dezembro"        (fecha dia 20 NO MÊS de dezembro -> a fatura que fecha em dezembro)
  "fatura do C6 de dezembro fecha 20/11"   (data completa)
  "fatura de janeiro do Itaú fecha dia 20" (mês = mês de VENCIMENTO da fatura, como o app rotula as faturas)
O app mostra o que vai mudar e só grava depois do "sim". O ajuste vale só para aquele ciclo e some assim que o banco informar
o fechamento real. Lançamentos MANUAIS de crédito (sem transação do banco) que mudam de fatura são movidos, se o usuário aceitar.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from calendar import monthrange
from datetime import date, timedelta

import app.database as db
from app import dados_banco as dbk
from app import faturas as fat

logger = logging.getLogger(__name__)

TTL_SEGUNDOS = 15 * 60
_PENDENTES: dict[str, dict] = {}
_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
          "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_GENERICAS = {"visa", "master", "mastercard", "cartao", "credito", "card", "gold", "platinum", "black", "infinite", "elo", "de", "do", "da"}
_FMT = lambda v: f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _norm(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(t or "")) if unicodedata.category(c) != "Mn").lower().strip()


def _label(mes_ref: date) -> str:
    return f"{_ABREV[mes_ref.month - 1]}/{mes_ref.year}"


def _dm(d: date) -> str:
    return d.strftime("%d/%m")


# ── reconhecer o comando ─────────────────────────────────────────────────────
_RE_FECHA = re.compile(r"\bfecha\w*\b")
_RE_DATA = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_RE_DIA = re.compile(r"\bdia\s+(\d{1,2})\b|\bfecha\w*\s+(?:em\s+|no\s+)?(\d{1,2})\b")


def eh_comando(texto: str) -> bool:
    """Frase afirmativa que diz que a fatura fecha em certo dia. Pergunta ('quando fecha...?') segue para o agente."""
    t = _norm(texto)
    if "?" in t or len(t) > 160 or not _RE_FECHA.search(t):
        return False
    return bool(_RE_DATA.search(t) or _RE_DIA.search(t))


def _ano_mais_proximo(mes: int, dia: int, hoje: date) -> date:
    melhor = None
    for ano in (hoje.year - 1, hoje.year, hoje.year + 1):
        d = date(ano, mes, min(dia, monthrange(ano, mes)[1]))
        if melhor is None or abs((d - hoje).days) < abs((melhor - hoje).days):
            melhor = d
    return melhor


def _achar_cartao(t: str, cartoes: list[dict]) -> list[dict]:
    achados = []
    for c in cartoes:
        palavras = [w for w in re.split(r"[^a-z0-9]+", _norm(c["nome"])) if len(w) >= 2 and w not in _GENERICAS]
        if any(re.search(rf"\b{re.escape(w)}\b", t) for w in palavras):
            achados.append(c)
    return achados


def _interpretar(t: str, hoje: date) -> dict | None:
    """Texto normalizado -> {'fech': date} (data completa) | {'dia': n, 'mes_fech': date1} | {'dia': n, 'mes_venc': date1} | {'dia': n, 'aberta': True}."""
    m = _RE_DATA.search(t)
    if m:
        d, mes = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mes <= 12):
            return None
        if m.group(3):
            ano = int(m.group(3)); ano += 2000 if ano < 100 else 0
            return {"fech": date(ano, mes, min(d, monthrange(ano, mes)[1]))}
        return {"fech": _ano_mais_proximo(mes, d, hoje)}
    m = _RE_DIA.search(t)
    if not m:
        return None
    dia = int(m.group(1) or m.group(2))
    if not 1 <= dia <= 31:
        return None
    nome_mes = "|".join(_MESES)
    mv = re.search(rf"\bfatura\s+(?:de\s+|do\s+mes\s+de\s+|em\s+)?({nome_mes})\b|\b({nome_mes})\s+fatura\b", t)
    if mv:
        mes = _MESES[mv.group(1) or mv.group(2)]
        ref = _ano_mais_proximo(mes, 1, hoje)
        return {"dia": dia, "mes_venc": ref}
    mf = re.search(rf"\b({nome_mes})\b", t)
    if mf:
        return {"dia": dia, "mes_fech": _ano_mais_proximo(_MESES[mf.group(1)], 1, hoje)}
    if re.search(r"\b(este|esse|neste|nesse)\s+mes\b", t):
        return {"dia": dia, "mes_fech": hoje.replace(day=1)}
    if re.search(r"\b(mes\s+que\s+vem|proximo\s+mes)\b", t):
        return {"dia": dia, "mes_fech": (hoje.replace(day=28) + timedelta(days=5)).replace(day=1)}
    if re.search(r"\b(atual|aberta|essa\s+fatura|esta\s+fatura|proxima\s+fatura)\b", t):
        return {"dia": dia, "aberta": True}
    return None


# ── localizar o ciclo e prever o que muda ────────────────────────────────────
def _ciclo_alvo(cartao: dict, pedido: dict, hoje: date):
    """(ciclo_original, fechamento_novo) ou (None, mensagem de erro)."""
    base = fat.ciclos_ajustados(cartao["ciclos"], hoje + timedelta(days=400), None, extra=1)     # SEM ajustes: identifica o ciclo
    if "fech" in pedido:
        novo = pedido["fech"]
        ciclo = min(base, key=lambda c: abs((c.fechamento - novo).days))
        if abs((ciclo.fechamento - novo).days) > 20:
            return None, (f"Não achei fatura do {cartao['nome']} com fechamento perto de {_dm(novo)}. "
                          f"O fechamento normal é por volta de {_dm(ciclo.fechamento)}.")
    elif "mes_venc" in pedido:
        ciclo = next((c for c in base if c.mes_ref == pedido["mes_venc"].replace(day=1)), None)
        if not ciclo:
            return None, f"Não achei a fatura de {_label(pedido['mes_venc'])} do {cartao['nome']}."
        mes_f = ciclo.vencimento.replace(day=1) - timedelta(days=1)
        novo = date(mes_f.year, mes_f.month, min(pedido["dia"], monthrange(mes_f.year, mes_f.month)[1]))
    elif "mes_fech" in pedido:
        ref = pedido["mes_fech"]
        novo = date(ref.year, ref.month, min(pedido["dia"], monthrange(ref.year, ref.month)[1]))
        ciclo = min(base, key=lambda c: abs((c.fechamento - novo).days))
        if abs((ciclo.fechamento - novo).days) > 20:
            return None, (f"Não achei fatura do {cartao['nome']} fechando perto de {_dm(novo)}. "
                          f"O fechamento normal é por volta de {_dm(ciclo.fechamento)}.")
    else:  # fatura aberta
        r = fat.ciclo_da_compra(hoje, base, hoje)
        ciclo = r[0]
        novo = date(ciclo.fechamento.year, ciclo.fechamento.month, min(pedido["dia"], monthrange(ciclo.fechamento.year, ciclo.fechamento.month)[1]))
    if not (ciclo.estimado or ciclo.fechamento_estimado):
        return None, (f"O banco já informou a fatura {_label(ciclo.mes_ref)} do {cartao['nome']}: fecha em {ciclo.fechamento.strftime('%d/%m/%Y')}. "
                      "Vale a data do banco, não preciso de ajuste.")
    anterior = max((c.fechamento for c in base if c.vencimento < ciclo.vencimento), default=None)
    if novo >= ciclo.vencimento or (anterior and novo <= anterior):
        return None, f"{novo.strftime('%d/%m/%Y')} não faz sentido para essa fatura (vence em {ciclo.vencimento.strftime('%d/%m')}). Confira a data."
    return ciclo, novo


def _mudancas(user_phone: str, cartao: dict, ciclo, novo: date, so_um_cartao: bool) -> tuple[list[dict], int]:
    """Lançamentos MANUAIS de crédito que trocam de fatura com o novo fechamento. (mudanças, quantos sem cartão identificado)."""
    ajustes_old = dict(cartao.get("ajustes") or {})
    ajustes_new = {**ajustes_old, ciclo.mes_ref: novo}
    antigo = fat.ciclos_ajustados(cartao["ciclos"], ciclo.fechamento, ajustes_old, extra=1)
    f_old = next((c.fechamento for c in antigo if c.mes_ref == ciclo.mes_ref), ciclo.fechamento)
    lo, hi = min(f_old, novo), max(f_old, novo)
    res = (db.get_db().table("finbot_expenses")
           .select("id, amount, description, purchase_date, billing_date, installment_of, installment_total, account_id")
           .ilike("user_phone", db._q(user_phone)).eq("payment_method", "credito").is_("pluggy_transaction_id", "null")
           .gte("purchase_date", (lo + timedelta(days=1)).isoformat()).lte("purchase_date", hi.isoformat()).execute())
    mudancas, sem_cartao = [], 0
    for r in res.data or []:
        if r.get("account_id") and r["account_id"] != cartao["id"]:
            continue
        if not r.get("account_id") and not so_um_cartao:
            sem_cartao += 1
            continue
        n, idx = int(r.get("installment_total") or 1), int(r.get("installment_of") or 1) - 1
        d = date.fromisoformat(str(r["purchase_date"])[:10])
        a = fat.ciclos_da_compra_ajustada(d, cartao["ciclos"], n, ajustes_old)
        b = fat.ciclos_da_compra_ajustada(d, cartao["ciclos"], n, ajustes_new)
        if not a or not b or idx >= len(a) or idx >= len(b):
            continue
        if a[idx].mes_ref != b[idx].mes_ref:
            mudancas.append({"id": r["id"], "valor": float(r["amount"]), "desc": r.get("description") or "",
                             "de": a[idx].mes_ref, "para": b[idx].mes_ref})
    return mudancas, sem_cartao


# ── conversa ─────────────────────────────────────────────────────────────────
def obter(user_phone: str, agora: float | None = None) -> dict | None:
    p = _PENDENTES.get(user_phone)
    if p and (agora or time.time()) - p["ts"] > TTL_SEGUNDOS:
        _PENDENTES.pop(user_phone, None)
        return None
    return p


def tem_pendente(user_phone: str) -> bool:
    return obter(user_phone) is not None


def limpar(user_phone: str) -> None:
    _PENDENTES.pop(user_phone, None)


def responder(user_phone: str, texto: str, hoje: date | None = None, cartoes: list[dict] | None = None) -> str:
    """Entende o comando e mostra o que vai mudar. NÃO grava: fica pendente até o 'sim'."""
    hoje = hoje or dbk.hoje_local()
    t = _norm(texto)
    cartoes = dbk.cartoes_credito(user_phone) if cartoes is None else cartoes
    if not cartoes:
        return "Ainda não tenho faturas de cartão guardadas para ajustar. Conecte o cartão ou digite *sincronizar* primeiro."
    achados = _achar_cartao(t, cartoes)
    if len(achados) == 1:
        cartao = achados[0]
    elif len(cartoes) == 1:
        cartao = cartoes[0]
    else:
        nomes = ", ".join(c["nome"] for c in cartoes)
        return f"De qual cartão? Tenho: {nomes}. Escreva de novo com o nome, ex.: *{cartoes[0]['nome'].split()[0]} fecha dia 20 em dezembro*."
    pedido = _interpretar(t, hoje)
    if not pedido:
        return ("Entendi que é sobre o fechamento, mas faltou o mês. Ex.: *o Itaú fecha dia 20 em dezembro* ou "
                "*fatura do C6 de dezembro fecha 20/11*.")
    ciclo, novo = _ciclo_alvo(cartao, pedido, hoje)
    if ciclo is None:
        return novo
    antigo = fat.ciclos_ajustados(cartao["ciclos"], ciclo.fechamento, cartao.get("ajustes"), extra=1)
    f_atual = next((c.fechamento for c in antigo if c.mes_ref == ciclo.mes_ref), ciclo.fechamento)
    if novo == f_atual:
        return f"Já considero que a fatura {_label(ciclo.mes_ref)} do {cartao['nome']} fecha em {_dm(novo)} 👍"
    mud, sem_cartao = _mudancas(user_phone, cartao, ciclo, novo, so_um_cartao=len(cartoes) == 1)
    _PENDENTES[user_phone] = {"cartao": cartao, "mes_ref": ciclo.mes_ref, "novo": novo, "mudancas": mud, "ts": time.time()}
    msg = (f"💳 *{cartao['nome']}* · fatura {_label(ciclo.mes_ref)} (vence {ciclo.vencimento.strftime('%d/%m')})\n"
           f"Fechamento: *{_dm(novo)}* (eu considerava {_dm(f_atual)}).")
    if mud:
        total = sum(m["valor"] for m in mud)
        de, para = _label(mud[0]["de"]), _label(mud[0]["para"])
        msg += (f"\n\nIsso muda a fatura de *{len(mud)} lançamento(s)* seus no crédito (R$ {_FMT(total)}), de {de} para {para}.")
        if sem_cartao:
            msg += f"\n(Mais {sem_cartao} lançamento(s) antigos sem cartão identificado não serão mexidos.)"
        return msg + "\n\nResponda *sim* para ajustar e mover, *só ajuste* para não mover ou *não*."
    if sem_cartao:
        msg += f"\n\nHá {sem_cartao} lançamento(s) antigos sem cartão identificado na janela; não serão mexidos."
    return msg + "\n\nNenhum lançamento seu muda de fatura. Responda *sim* para guardar ou *não*."


def interpretar_resposta(texto: str) -> str | None:
    t = _norm(texto).strip(" .!?")
    if t in ("sim", "s", "ok", "confirmo", "pode", "isso", "mover"):
        return "ok"
    if t in ("so ajuste", "apenas ajuste", "ajuste", "so o ajuste", "sem mover", "nao mover", "so ajustar"):
        return "ajuste"
    if t in ("nao", "n", "cancelar", "cancela"):
        return "no"
    return None


def confirmar(user_phone: str, acao: str) -> str:
    p = obter(user_phone)
    if not p:
        return "Essa confirmação expirou 😉 Me diga de novo o fechamento."
    limpar(user_phone)                     # uma resposta = uma gravação
    if acao == "no":
        return "Ok, não mudei nada."
    cartao, mud = p["cartao"], p["mudancas"] if acao == "ok" else []
    try:
        dbk.gravar_ajuste(user_phone, cartao["id"], p["mes_ref"], p["novo"])
        for m in mud:
            db.get_db().table("finbot_expenses").update({"billing_date": m["para"].isoformat()}).eq("id", m["id"]).execute()
    except Exception as exc:
        logger.error(f"Ajuste de fechamento falhou ({type(exc).__name__}: {exc})")
        return "⚠️ Não consegui gravar o ajuste agora. Tente de novo em instantes."
    msg = f"✅ Fatura {_label(p['mes_ref'])} do {cartao['nome']} agora fecha em {_dm(p['novo'])}."
    if mud:
        msg += f" {len(mud)} lançamento(s) movido(s) para {_label(mud[0]['para'])}."
    return msg + "\nQuando o banco informar o fechamento real, vale a data dele."
