"""
Diagnóstico (só leitura): o que a Pluggy devolve de lançamentos do cartão C6 a partir de uma data, com status e parcela.

    python diag_c6_transacoes.py --user-key 890a772cf33d0238 [--desde 2026-10-01] [--banco c6]

Mostra data, status (PENDING/POSTED), valor, parcela e se veio billId; descrição truncada a 14 letras.
Depois compare com o que está em finbot_expenses (SQL no chat). Não grava nada.
"""
import asyncio
import sys
from collections import Counter
from datetime import date, timedelta

import requests


async def main(user_key: str, desde: str, banco: str) -> None:
    import app.database as db
    from app.pluggy_service import PluggyService
    svc = PluggyService()
    contas = [c for c in (db.get_db().table("finbot_contas").select("account_id, tipo, nome, banco_nome")
                          .eq("user_key", user_key).execute().data or [])
              if (c.get("tipo") or "").upper() == "CREDIT" and banco.lower() in f"{c.get('banco_nome')} {c.get('nome')}".lower()]
    if not contas:
        sys.exit("Nenhum cartão encontrado com esse filtro.")
    for c in contas:
        print(f"=== {c.get('banco_nome')} / {c.get('nome')} ===")
        r = requests.get(f"{svc.base_url}/v2/transactions", headers=svc.headers, timeout=30,
                         params={"accountId": c["account_id"], "dateFrom": desde,
                                 "dateTo": (date.today() + timedelta(days=60)).isoformat()})
        if not r.ok:
            sys.exit(f"Pluggy respondeu {r.status_code}: {r.text[:300]}")
        txs = sorted(r.json().get("results", []), key=lambda t: str(t.get("date")))
        print(f"{len(txs)} lançamentos desde {desde} | status: {dict(Counter(t.get('status') for t in txs))}\n")
        for t in txs:
            cm = t.get("creditCardMetadata") or {}
            parc = f"{cm.get('installmentNumber')}/{cm.get('totalInstallments')}" if cm.get("totalInstallments") else "-"
            print(f"{str(t.get('date'))[:10]}  {str(t.get('status')):7}  R$ {t.get('amount'):>9}  parcela {parc:6}  "
                  f"billId {'sim' if cm.get('billId') else 'não'}  {str(t.get('description'))[:14]}")


if __name__ == "__main__":
    if "--user-key" not in sys.argv:
        sys.exit("uso: python diag_c6_transacoes.py --user-key <16 caracteres> [--desde AAAA-MM-DD] [--banco c6]")
    a = sys.argv
    asyncio.run(main(a[a.index("--user-key") + 1],
                     a[a.index("--desde") + 1] if "--desde" in a else "2026-10-01",
                     a[a.index("--banco") + 1] if "--banco" in a else "c6"))
