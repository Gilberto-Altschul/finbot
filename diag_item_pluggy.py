"""
Diagnóstico do item (conexão) de cada cartão na Pluggy: status, última atualização e erro, sem imprimir dados pessoais.

    python diag_item_pluggy.py --user-key 890a772cf33d0238
    python diag_item_pluggy.py --user-key 890a772cf33d0238 --atualizar     # pede à Pluggy para buscar dados novos agora

Só LÊ, a não ser que você passe --atualizar. A Pluggy limita o pedido de atualização a 1 por hora por item
em aplicativos novos; se der erro 429 ou parecido, tente mais tarde.
"""
import asyncio
import sys

import requests


async def main(user_key: str, atualizar: bool) -> None:
    import app.database as db
    from app.pluggy_service import PluggyService
    svc = PluggyService()
    contas = [c for c in (db.get_db().table("finbot_contas").select("account_id, item_id, tipo, nome, banco_nome")
                          .eq("user_key", user_key).execute().data or []) if (c.get("tipo") or "").upper() == "CREDIT"]
    if not contas:
        sys.exit(f"Nenhum cartão em finbot_contas para user_key={user_key}.")
    vistos = set()
    for c in contas:
        item_id = c.get("item_id")
        print(f"=== {c.get('banco_nome')} / {c.get('nome')} ===")
        if not item_id or item_id in vistos:
            print("   (sem item_id ou já mostrado)\n")
            continue
        vistos.add(item_id)
        r = requests.get(f"{svc.base_url}/items/{item_id}", headers=svc.headers, timeout=30)
        if r.status_code != 200:
            print(f"   erro {r.status_code} ao ler o item\n")
            continue
        d = r.json()
        print(f"   status: {d.get('status')} | executionStatus: {d.get('executionStatus')}")
        print(f"   última atualização (lastUpdatedAt): {d.get('lastUpdatedAt')}")
        print(f"   criado em: {d.get('createdAt')} | atualizado em: {d.get('updatedAt')}")
        err = d.get("error") or {}
        if err:
            print(f"   erro do conector: {err.get('code')} - {err.get('message')}")
        if atualizar:
            try:
                await svc.atualizar_item(item_id)
                print("   pedido de atualização enviado; aguarde uns minutos e rode o diag_faturas_pluggy.py de novo.")
            except Exception as e:
                print(f"   não consegui pedir a atualização: {e}")
        print()


if __name__ == "__main__":
    if "--user-key" not in sys.argv:
        sys.exit("uso: python diag_item_pluggy.py --user-key <16 caracteres> [--atualizar]")
    asyncio.run(main(sys.argv[sys.argv.index("--user-key") + 1], "--atualizar" in sys.argv))
