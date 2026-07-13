import sys
import requests
from datetime import date
from app.config import get_settings

BASE_URL = "https://api.pluggy.ai"

def step(msg: str):
    print(f"\n{'=' * 60}\n{msg}\n{'=' * 60}")

def main():
    settings = get_settings()

    client_id = settings.pluggy_client_id
    client_secret = settings.pluggy_client_secret
    item_id = settings.default_item_id

    if not client_id or not client_secret:
        print("❌ PLUGGY_CLIENT_ID ou PLUGGY_CLIENT_SECRET não configurados no .env")
        sys.exit(1)

    # 1. Autenticação
    step("1. Autenticando em /auth")
    try:
        resp = requests.post(
            f"{BASE_URL}/auth",
            json={"clientId": client_id, "clientSecret": client_secret},
            timeout=15,
        )
        resp.raise_for_status()
        api_key = resp.json()["apiKey"]
        print(f"✅ Autenticado com sucesso. apiKey: {api_key[:6]}...{api_key[-4:]}")
    except requests.exceptions.HTTPError as e:
        print(f"❌ Falha na autenticação: {e}")
        print(f"   Resposta: {resp.text}")
        sys.exit(1)

    headers = {"accept": "application/json", "x-api-key": api_key}

    # 2. Valida o item
    if item_id:
        step(f"2. Verificando item {item_id}")
        try:
            resp = requests.get(f"{BASE_URL}/items/{item_id}", headers=headers, timeout=15)
            resp.raise_for_status()
            item = resp.json()
            print(f"✅ Item encontrado. Conector: {item.get('connector', {}).get('name')} | Status: {item.get('status')}")
        except requests.exceptions.HTTPError as e:
            print(f"⚠️  Não foi possível acessar o item: {e}")
            print(f"   Resposta: {resp.text}")
    else:
        print("\n⚠️  DEFAULT_ITEM_ID não configurado — pulando etapa 2.")

    # 3. Lista contas
    account_ids = []
    if item_id:
        step("3. Listando contas do item")
        try:
            resp = requests.get(f"{BASE_URL}/accounts", headers=headers, params={"itemId": item_id}, timeout=15)
            resp.raise_for_status()
            accounts = resp.json().get("results", [])
            if not accounts:
                print("⚠️  Nenhuma conta encontrada para esse item.")
            for acc in accounts:
                account_ids.append(acc["id"])
                print(f"  • {acc.get('name')} ({acc.get('type')}) — id: {acc['id']}")
        except requests.exceptions.HTTPError as e:
            print(f"❌ Falha ao listar contas: {e}")
            print(f"   Resposta: {resp.text}")

    # 4. Puxa 1 transação de cada conta
    if account_ids:
        step("4. Testando /v2/transactions em cada conta")
        for acc_id in account_ids:
            try:
                resp = requests.get(
                       f"{BASE_URL}/v2/transactions", 
                       headers=headers,
                       params={"accountId": acc_id, "dateFrom": "2025-01-01"},
                       timeout=15, 
                )
                
                if resp.status_code != 200:
                    print(f"  ❌ Conta {acc_id}: erro {resp.status_code}")
                    print(f"     Resposta: {resp.text}")
                    continue

                results = resp.json().get("results", [])
                if results:
                    tx = results[0]
                    print(f"  ✅ Conta {acc_id}: transação -> {tx.get('description')} ({tx.get('amount')})")
                else:
                    print(f"  ⚠️  Conta {acc_id}: nenhuma transação retornada.")
            except Exception as e:
                print(f"  ❌ Conta {acc_id}: erro inesperado -> {e}")

    print("\n✅ Sanity check concluído.")

if __name__ == "__main__":
    main()
