# billing.py
# ─────────────────────────────────────────────────────────────────────────────
# Lógica do ciclo de fatura do cartão de crédito.
#
# Regra:
#   - Compras até o dia de corte (inclusive) → fatura que vence no mês seguinte
#   - Compras após o dia de corte → fatura que vence em dois meses
#
# A fatura é calculada SEMPRE na consulta, nunca no registro.
# Configuração é por usuário (tabela finbot_user_settings).
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from calendar import monthrange
from datetime import date


MONTHS_PT = [
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _add_months(d: date, months: int) -> date:
    """Add N months to a date, clamping to the last day of the target month."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def fatura_vencimento(purchase_date: date, dia_corte: int, dia_vencimento: int) -> date:
    """
    Given a purchase date and the user's card settings,
    return the due date of the invoice it belongs to.

    Example (corte=24, vencimento=1):
      10/05 → 01/06
      24/05 → 01/06
      25/05 → 01/07
    """
    if purchase_date.day <= dia_corte:
        base = _add_months(purchase_date, 1)
    else:
        base = _add_months(purchase_date, 2)

    last_day = monthrange(base.year, base.month)[1]
    due_day = min(dia_vencimento, last_day)
    return date(base.year, base.month, due_day)


def fatura_label(due_date: date) -> str:
    """Human-readable label: 'fatura junho/2026'"""
    return f"fatura {MONTHS_PT[due_date.month]}/{due_date.year}"


def parcelas(
    purchase_date: date,
    total: float,
    n: int,
    dia_corte: int,
    dia_vencimento: int,
) -> list[dict]:
    """
    Split a credit purchase into N installments across consecutive invoices.
    Returns list of dicts: valor, fatura_vencimento, fatura_label, parcela, total_parcelas.
    """
    valor_parcela = round(total / n, 2)
    resultado = []
    acumulado = 0.0

    for i in range(n):
        data_parcela = (
            date(
                _add_months(purchase_date, i).year,
                _add_months(purchase_date, i).month,
                purchase_date.day,
            )
            if i > 0
            else purchase_date
        )
        due = fatura_vencimento(data_parcela, dia_corte, dia_vencimento)
        valor = valor_parcela if i < n - 1 else round(total - acumulado, 2)
        acumulado += valor

        resultado.append({
            "parcela": i + 1,
            "total_parcelas": n,
            "valor": valor,
            "fatura_vencimento": due.isoformat(),
            "fatura_label": fatura_label(due),
        })

    return resultado
