"""Projeção das parcelas que ainda vão cair nos próximos meses.

A Pluggy só entrega a parcela do próximo mês (ou nenhuma, como no C6: ela só aparece quando o banco lança a fatura).
Como cada compra parcelada traz número da parcela, total e valor, as que faltam são calculadas: uma por mês, a partir
do mês seguinte à última parcela já registrada. É projeção (não grava nada): o valor real pode mudar por juros ou estorno.
"""
from __future__ import annotations
import re
import unicodedata
from datetime import date


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _mes(d: str | None) -> date | None:
    try:
        return date.fromisoformat(str(d)[:7] + "-01")
    except (ValueError, TypeError):
        return None


def _soma_meses(m: date, n: int) -> date:
    k = m.month - 1 + n
    return date(m.year + k // 12, k % 12 + 1, 1)


_SUFIXO_PARCELA = re.compile(r"\s*\(?\b(?:parcela\s*)?\d{1,2}\s*(?:/|de)\s*\d{1,2}\)?\s*$", re.I)


def _sem_sufixo_parcela(descricao: str | None) -> str:
    """Alguns bancos (Nubank) escrevem a parcela na própria descrição ("Amazonmktplc*Ankerinno 2/3"). Sem tirar isso,
    cada parcela virava uma "compra" diferente e a projeção somava de novo parcelas que já estão gravadas."""
    return _SUFIXO_PARCELA.sub("", descricao or "").strip()


def valor_demais_parcelas(primeira: float, total: int) -> float:
    """Só se conhece a 1ª parcela. Quando o banco põe a sobra do arredondamento nela (ex.: 98,35 + 5 x 98,33 = 590,00),
    as demais valem `primeira - r` centavos, com 0 < r < total, desde que o total da compra seja em reais inteiros.
    Sem esse padrão (ou se r=0 já fecha), devolve a própria 1ª parcela."""
    p = round(primeira * 100)
    if total <= 1 or (total * p) % 100 == 0:
        return primeira
    for r in range(1, total):
        if (total * (p - r) + r) % 100 == 0:
            return round((p - r) / 100, 2)
    return primeira


def projetar(compras: list[dict], hoje: date | None = None) -> dict:
    """compras: linhas de finbot_expenses com description, amount, installment_of, installment_total, billing_date.
    Devolve {"meses": [(date, total, n_parcelas)], "series": [{descricao, valor, faltam, atual, total, ultimo_mes}], "total": float}."""
    hoje = hoje or date.today()
    mes_atual = hoje.replace(day=1)
    series: dict[tuple, dict] = {}
    for c in compras:
        n, t, bm = c.get("installment_of"), c.get("installment_total"), _mes(c.get("billing_date"))
        if not n or not t or t <= 1 or bm is None:
            continue
        chave = (_norm(_sem_sufixo_parcela(c.get("description")))[:24], int(t), round(float(c["amount"])))
        atual = series.get(chave)
        if atual is None or (bm, int(n)) > (atual["mes"], atual["n"]):
            series[chave] = {"descricao": c.get("description") or "", "valor": float(c["amount"]), "n": int(n), "total": int(t), "mes": bm}
    meses: dict[date, list] = {}
    detalhe: dict[date, list] = {}
    saida = []
    for s in series.values():
        faltam = s["total"] - s["n"]
        if faltam <= 0:
            continue
        if s["n"] == 1:
            s = {**s, "valor": valor_demais_parcelas(s["valor"], s["total"])}
        for i in range(1, faltam + 1):
            m = _soma_meses(s["mes"], i)
            if m <= mes_atual and m < s["mes"]:
                continue
            meses.setdefault(m, []).append(s["valor"])
            detalhe.setdefault(m, []).append((s["descricao"], s["valor"], s["n"] + i, s["total"]))
        ultimo = _soma_meses(s["mes"], faltam)
        if ultimo >= mes_atual:
            saida.append({"descricao": s["descricao"], "valor": s["valor"], "faltam": faltam, "atual": s["n"], "total": s["total"], "ultimo_mes": ultimo})
    lista = sorted(((m, round(sum(v), 2), len(v)) for m, v in meses.items() if m >= mes_atual))
    return {"meses": lista, "series": sorted(saida, key=lambda x: (x["ultimo_mes"], -x["valor"])), "total": round(sum(x[1] for x in lista), 2),
            "detalhe": {m: sorted(v, key=lambda x: -x[1]) for m, v in detalhe.items()}}


def previstas_do_mes(compras: list[dict], mes: date, hoje: date | None = None) -> tuple[float, int]:
    """(total, quantidade) das parcelas que ainda vão cair em `mes` (1º dia do mês de vencimento) e não estão gravadas."""
    for m, tot, n in projetar(compras, hoje)["meses"]:
        if m == mes:
            return tot, n
    return 0.0, 0


def previstas_detalhe(compras: list[dict], mes: date, hoje: date | None = None) -> list[tuple]:
    """[(descrição, valor, nº da parcela, total de parcelas)] previstas para `mes`, da maior para a menor."""
    return projetar(compras, hoje)["detalhe"].get(mes, [])


def _brl(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def mensagem(compras: list[dict], hoje: date | None = None, max_meses: int = 12, max_series: int = 12) -> str:
    p = projetar(compras, hoje)
    if not p["meses"]:
        return "💳 Não encontrei parcelas futuras nas suas compras parceladas."
    linhas = [f" • {_MESES[m.month - 1]}/{str(m.year)[2:]}: R$ {_brl(tot)} ({n} parcela{'s' if n != 1 else ''})" for m, tot, n in p["meses"][:max_meses]]
    msg = ("💳 *Parcelas que ainda vão cair* (projeção)\n\n" + "\n".join(linhas) +
           f"\n\n💰 *Total ainda a pagar:* R$ {_brl(p['total'])}\n")
    ativos = p["series"][:max_series]
    if ativos:
        msg += "\n🧾 *Compras em andamento:*\n" + "\n".join(
            f" • {s['descricao'][:22]}: R$ {_brl(s['valor'])} · faltam {s['faltam']} (até {_MESES[s['ultimo_mes'].month - 1]}/{str(s['ultimo_mes'].year)[2:]})"
            for s in ativos)
        if len(p["series"]) > max_series:
            msg += f"\n … e mais {len(p['series']) - max_series}."
    msg += "\n\nℹ️ Calculado pelo número da parcela e pelo valor; o banco pode ajustar."
    return msg
