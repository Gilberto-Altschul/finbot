"""
Diagnostico de PARCELAS e FECHAMENTO da fatura (somente leitura).

Mostra o que a Pluggy REALMENTE manda para as transacoes parceladas e para as do fim do ciclo, para comparar com o que o
FinBot gravou (billing_date, installment_of/total). Nao imprime numero de conta, CPF nem nome; a descricao sai cortada (14 letras).

Uso (PowerShell, na pasta do projeto, com o .env de sempre):
    python diagnostico_parcelas.py 334e7c63-972b-4132-8fc9-deeb738b0180
(o argumento e o account_id do cartao; sem argumento, lista as contas de cartao de todos os itens)
Opcional:  --de 2026-08-01 --ate 2026-11-30
Cole a saida inteira no chat.
"""
import os, sys, json, requests
from datetime import date, timedelta

BASE = "https://api.pluggy.ai"


def carregar_env(caminho=".env"):
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8-sig") as f:
        for linha in f:
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                os.environ.setdefault(k.strip().upper(), v.strip().strip('"').strip("'"))


def get(path, h, params=None):
    r = requests.get(BASE + path, headers=h, params=params, timeout=30)
    return r.json() if r.status_code == 200 else {"_http": r.status_code, "_corpo": r.text[:200]}


def todas(path, h, params):
    """/v2/transactions usa cursor (o /transactions antigo foi desligado: HTTP 410). Segue o cursor ate acabar."""
    out, cursor, pag = [], None, 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        j = get(path, h, p)
        if "_http" in j:
            print("  erro:", j); break
        pag += 1
        if pag == 1:
            print("  resposta da 1a pagina: chaves =", sorted(j.keys()), "| itens =", len(j.get("results", [])),
                  "| paginacao =", {k: (v if not isinstance(v, (list, dict)) else "...") for k, v in j.items() if k != "results"})
        out += j.get("results", [])
        cursor = j.get("next") or j.get("nextCursor") or j.get("cursor") or (j.get("pagination") or {}).get("nextCursor")
        if not cursor or pag > 50:
            break
    print(f"  total lido: {len(out)} em {pag} pagina(s)")
    return out


def main():
    carregar_env()
    args = sys.argv[1:]
    def opt(nome, padrao):
        return args[args.index(nome) + 1] if nome in args else padrao
    de, ate = opt("--de", "2026-08-01"), opt("--ate", "2026-11-30")
    pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    r = requests.post(f"{BASE}/auth", json={"clientId": os.environ["PLUGGY_CLIENT_ID"], "clientSecret": os.environ["PLUGGY_CLIENT_SECRET"]}, timeout=30)
    r.raise_for_status()
    h = {"X-API-KEY": r.json()["apiKey"], "accept": "application/json"}

    contas = pos
    if not contas:
        for it in get("/items", h).get("results", []):
            for ac in get("/accounts", h, {"itemId": it["id"]}).get("results", []):
                if (ac.get("type") or "").upper() == "CREDIT":
                    print(f"cartao: account_id={ac['id']}  item={it['id']}  conector={((it.get('connector') or {}).get('name'))}")
        print("\nRode de novo com o account_id do Santander como argumento.")
        return

    for acc in contas:
        print(f"\n================ conta {acc} ================")
        bills = get("/bills", h, {"accountId": acc}).get("results", [])
        print("\n--- FATURAS (vencimento, fechamento, total) ---")
        for b in sorted(bills, key=lambda b: b.get("dueDate") or ""):
            print(f"  venc {str(b.get('dueDate'))[:10]}  fecha {str(b.get('billClosingDate') or '?')[:10]}  total {b.get('totalAmount')}  id {str(b.get('id'))[:8]}")

        txs = todas("/v2/transactions", h, {"accountId": acc, "dateFrom": de, "dateTo": ate})
        print(f"\n--- {len(txs)} transacoes entre {de} e {ate} ---")
        print("data compra | valor    | tipo   | parcela | creditCardDate | billId   | status  | descricao")
        mostrar = []
        for t in txs:
            m = t.get("creditCardMetadata") or {}
            parc = m.get("installmentNumber") is not None or (m.get("totalInstallments") or 1) > 1
            d = (t.get("date") or "")[:10]
            if parc or "2026-09-20" <= d <= "2026-10-05":
                mostrar.append((d, t, m))
        for d, t, m in sorted(mostrar, key=lambda x: x[0], reverse=True):
            parc = f"{m.get('installmentNumber')}/{m.get('totalInstallments')}" if m.get("totalInstallments") else "-"
            print(f"{d} | {float(t.get('amountInAccountCurrency') or t.get('amount') or 0):>8.2f} | {str(t.get('type')):<6} | {parc:>7} | "
                  f"{str(m.get('creditCardDate') or t.get('creditCardDate') or '-')[:10]:>14} | {str(m.get('billId') or '-')[:8]:<8} | "
                  f"{str(t.get('status') or '-'):<7} | {(t.get('description') or '')[:14]}")
        # MCC (tipo do estabelecimento) e categoria da propria Pluggy: vem preenchido?
        com_mcc = [t for t in txs if (t.get("creditCardMetadata") or {}).get("payeeMCC")]
        com_cat = [t for t in txs if t.get("category")]
        print(f"\n--- MCC: {len(com_mcc)} de {len(txs)} transacoes com payeeMCC preenchido; {len(com_cat)} com category da Pluggy ---")
        vistos = {}
        for t in txs:
            k = (t.get("description") or "")[:22]
            vistos.setdefault(k, ((t.get("creditCardMetadata") or {}).get("payeeMCC"), t.get("category")))
        print("descricao              | payeeMCC | categoria Pluggy")
        for k, (mcc, cat) in list(vistos.items())[:40]:
            print(f"{k:<22} | {str(mcc or '-'):<8} | {cat or '-'}")
        # campos que a Pluggy manda (nomes), para ver se ha algo de fatura que o FinBot nao usa
        if txs:
            exemplo = next((t for t in txs if (t.get("creditCardMetadata") or {}).get("totalInstallments")), txs[0])
            print("\n--- campos de uma transacao parcelada (valores omitidos) ---")
            print(json.dumps({k: ("..." if not isinstance(v, dict) else {kk: "..." for kk in v}) for k, v in exemplo.items()}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()