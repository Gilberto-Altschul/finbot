"""
Diagnostico Pluggy: o que o SEU plano/conector realmente entrega.

Uso (PowerShell):
    $env:PLUGGY_CLIENT_ID="..."; $env:PLUGGY_CLIENT_SECRET="..."
    python diagnostico_pluggy.py

Somente leitura. Mascara numeros de conta, CPF, nomes e descricoes.
Cole a saida inteira no chat (ela ja vem sem dados sensiveis).
"""
import os
import sys
import json
import requests

BASE = "https://api.pluggy.ai"
MASCARAR = {"number", "taxNumber", "owner", "marketingName", "name_holder",
            "agency", "branch", "cpf", "cnpj", "description", "descriptionRaw",
            "merchant", "paymentData", "creditCardMetadata", "cardNumber",
            "transferNumber", "identificationNumber", "accountNumber", "iban"}


def mask(v):
    if isinstance(v, dict):
        return {k: ("***" if k in MASCARAR and k not in ("name",) else mask(x))
                for k, x in v.items()}
    if isinstance(v, list):
        return [mask(x) for x in v]
    return v


def carregar_env(caminho=".env"):
    """Le o .env (se existir) sem depender de bibliotecas. Nao sobrescreve o ambiente."""
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            k = k.strip().upper()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def get(path, headers, params=None):
    r = requests.get(BASE + path, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        return {"_http": r.status_code, "_corpo": r.text[:300]}
    return r.json()


def show(titulo, obj):
    print(f"\n--- {titulo} ---")
    print(json.dumps(mask(obj), indent=2, ensure_ascii=False, default=str))


def main():
    carregar_env()
    cid = os.getenv("PLUGGY_CLIENT_ID")
    sec = os.getenv("PLUGGY_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("Nao achei PLUGGY_CLIENT_ID / PLUGGY_CLIENT_SECRET nem no ambiente "
                 "nem no arquivo .env desta pasta.")

    r = requests.post(f"{BASE}/auth", json={"clientId": cid, "clientSecret": sec}, timeout=30)
    r.raise_for_status()
    h = {"X-API-KEY": r.json()["apiKey"], "accept": "application/json"}

    resp = get("/items", h)
    if "_http" in resp:
        print(f"GET /items falhou: {resp}")
    itens = resp.get("results", []) if "_http" not in resp else []
    print(f"Itens retornados por GET /items: {len(itens)}")

    # Fallback: ids informados na linha de comando ou DEFAULT_ITEM_ID do .env
    ids = [a for a in sys.argv[1:]] or ([os.getenv("DEFAULT_ITEM_ID")] if os.getenv("DEFAULT_ITEM_ID") else [])
    for iid in ids:
        if any(i.get("id") == iid for i in itens):
            continue
        it = get(f"/items/{iid}", h)
        if "_http" in it:
            print(f"GET /items/{iid[:8]}... falhou: {it}")
        else:
            itens.append(it)
    print(f"Itens a analisar: {len(itens)}")

    for it in itens:
        c = it.get("connector", {})
        print("\n" + "=" * 60)
        print(f"CONECTOR: {c.get('name')} | tipo={c.get('type')} | "
              f"openFinance={c.get('isOpenFinance')} | pais={c.get('country')}")
        print(f"status={it.get('status')} executionStatus={it.get('executionStatus')} "
              f"lastUpdatedAt={it.get('lastUpdatedAt')}")
        print(f"produtos={it.get('products')}")
        if it.get("error"):
            show("erro do item", it["error"])

        contas = get("/accounts", h, {"itemId": it["id"]}).get("results", [])
        for ac in contas:
            print("\n  >> CONTA", ac.get("type"), ac.get("subtype"))
            show("conta (balance/creditData)", {
                "type": ac.get("type"), "subtype": ac.get("subtype"),
                "balance": ac.get("balance"), "currencyCode": ac.get("currencyCode"),
                "bankData": mask(ac.get("bankData")),
                "creditData": mask(ac.get("creditData")),
            })
            if ac.get("type") == "CREDIT":
                bills = get("/bills", h, {"accountId": ac["id"]})
                if "_http" in bills:
                    show("FATURAS /bills (ERRO)", bills)
                else:
                    res = bills.get("results", [])
                    print(f"  Faturas retornadas: {len(res)}")
                    show("FATURAS /bills (ate 6)", res[:6])
            tx = get("/v2/transactions", h, {"accountId": ac["id"], "limit": 1})
            print(f"  /v2/transactions respondeu: {'erro ' + str(tx.get('_http')) if '_http' in tx else 'ok'}")


if __name__ == "__main__":
    main()
