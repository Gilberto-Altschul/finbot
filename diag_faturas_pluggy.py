"""
Diagnóstico (só leitura): o que a Pluggy informa sobre as faturas de cartão e seus pagamentos.

    python diag_faturas_pluggy.py --user-key 0373fa8f1a099c9c

Para cada cartão em finbot_contas, lista as faturas da Pluggy (/bills): vencimento, fechamento, total e os
pagamentos que a Pluggy anexou (data e valor), e mostra o status que o app calcularia. Não grava nada.
Não imprime nomes nem números de cartão.
"""
import asyncio
import sys
from datetime import date


async def main(user_key: str) -> None:
    import app.database as db
    import app.faturas as fat
    from app.pluggy_service import PluggyService
    svc = PluggyService()
    cartoes = [c for c in (db.get_db().table("finbot_contas").select("account_id, tipo, nome, banco_nome")
                           .eq("user_key", user_key).execute().data or [])
               if (c.get("tipo") or "").upper() == "CREDIT"]
    if not cartoes:
        sys.exit(f"Nenhum cartão em finbot_contas para user_key={user_key}.")
    for c in cartoes:
        print(f"=== {c.get('banco_nome')} / {c.get('nome')} ===")
        bills = await svc.listar_faturas(c["account_id"])
        for b in sorted(bills, key=lambda b: str(b.get("dueDate") or ""), reverse=True)[:5]:
            print(f"  vence {str(b.get('dueDate'))[:10]} | fecha {str(b.get('billClosingDate'))[:10]} "
                  f"| total {b.get('totalAmount')}")
            pags = b.get("payments") or []
            if not pags:
                print("     pagamentos informados pela Pluggy: nenhum")
            for p in pags:
                print(f"     pagamento: {str(p.get('paymentDate'))[:10]}  R$ {p.get('amount')}")
        itens = fat.status_dos_ciclos(bills, date.today())
        print("  status calculado pelo app (só com o /bills):")
        for ci, st in sorted(itens, key=lambda x: x[0].vencimento, reverse=True)[:5]:
            print(f"     vence {ci.vencimento} total {ci.total} -> {st}")
        print()


if __name__ == "__main__":
    if "--user-key" not in sys.argv:
        sys.exit("uso: python diag_faturas_pluggy.py --user-key <16 caracteres>")
    asyncio.run(main(sys.argv[sys.argv.index("--user-key") + 1]))
