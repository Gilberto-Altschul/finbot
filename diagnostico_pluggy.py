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
from datetime import date, timedelta

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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def analisar_cartao(ac, bills, h):
    """Explica de onde pode vir a diferenca entre o 'saldo' do cartao e a fatura aberta.
    Imprime SO totais (nenhuma descricao de compra)."""
    cd = ac.get("creditData") or {}
    limite, disp = _num(cd.get("creditLimit")), _num(cd.get("availableCreditLimit"))
    print("\n  >> ANALISE DA FATURA ABERTA")
    print(f"  balance (saldo da conta) ........ {ac.get('balance')}")
    print(f"  creditLimit ..................... {limite}")
    print(f"  availableCreditLimit ............ {disp}")
    if limite is not None and disp is not None:
        print(f"  limite usado (limite - disp.) ... {round(limite - disp, 2)}")
    res = bills.get("results", []) if isinstance(bills, dict) and "_http" not in bills else []
    fechs = sorted((b.get("billClosingDate") or "")[:10] for b in res if b.get("billClosingDate"))
    if not fechs:
        print("  (sem billClosingDate nas faturas: nao da para delimitar o ciclo)")
        return
    ultimo = date.fromisoformat(fechs[-1])
    hoje = date.today()
    print(f"  ultimo fechamento informado ..... {ultimo}")
    d_de, d_ate = (ultimo + timedelta(days=1)).isoformat(), (hoje + timedelta(days=150)).isoformat()
    # Mesmos parametros que o FinBot usa (a API recusa page/pageSize/limit nesta rota).
    r = get("/v2/transactions", h, {"accountId": ac["id"], "dateFrom": d_de, "dateTo": d_ate})
    if "_http" in r:
        print(f"  /v2/transactions falhou: {r}")
        return
    txs = r.get("results", [])
    print(f"  campos da resposta: {sorted(k for k in r if k != 'results')} | recebidas: {len(txs)}")
    print(f"  valor de 'next' (se houver mais paginas): {r.get('next')!r}")
    print("  Lancamentos (sem descricao):  data | valor | type | status | metodo | parcela")
    for t in sorted(txs, key=lambda t: (t.get("date") or "")):
        meta = t.get("creditCardMetadata") or {}
        print(f"    {(t.get('date') or '')[:10]} | {t.get('amount')} | {t.get('type')} | {t.get('status')} | "
              f"{t.get('paymentMethod')} | {meta.get('installmentNumber')}/{meta.get('totalInstallments')}")
    grupos = {"ate hoje, parcela 1 ou a vista": 0.0, "ate hoje, parcelas 2+": 0.0,
              "datadas no futuro (parcelas)": 0.0, "pendentes (status PENDING)": 0.0}
    qtd = dict.fromkeys(grupos, 0)
    for t in txs:
        a = _num(t.get("amount")) or 0.0
        liquido = abs(a) if (t.get("type") or "").upper() == "DEBIT" else -abs(a)
        meta = t.get("creditCardMetadata") or {}
        parc = meta.get("installmentNumber") or 1
        data = (t.get("date") or "")[:10]
        if (t.get("status") or "").upper() == "PENDING":
            g = "pendentes (status PENDING)"
        elif data > hoje.isoformat():
            g = "datadas no futuro (parcelas)"
        elif parc > 1:
            g = "ate hoje, parcelas 2+"
        else:
            g = "ate hoje, parcela 1 ou a vista"
        grupos[g] += liquido
        qtd[g] += 1
    print(f"  Transacoes de {d_de} em diante (liquido = compras - estornos):")
    for g, v in grupos.items():
        print(f"    {g:<34} {round(v, 2):>10}  ({qtd[g]} lancamentos)")
    print(f"    {'TOTAL':<34} {round(sum(grupos.values()), 2):>10}")
    print("  -> compare com a fatura aberta que o app do banco mostra.")
    reconciliar_faturas_fechadas(ac, res, h)


def _brl(t):
    """Valor da compra em reais (converte compras em moeda estrangeira quando a Pluggy informa)."""
    if (t.get("currencyCode") or "BRL") != "BRL" and t.get("amountInAccountCurrency") is not None:
        return abs(float(t["amountInAccountCurrency"]))
    return abs(_num(t.get("amount")) or 0.0)


def reconciliar_faturas_fechadas(ac, bills, h):
    """Para cada fatura fechada: total do banco x soma das compras que a Pluggy entrega no ciclo.
    Se a falta se repete todo mes, sao itens (parcelas, tarifas) que a Pluggy nao manda como compra."""
    from datetime import date, timedelta
    cic = sorted(((b["billClosingDate"][:10], b["dueDate"][:10], float(b.get("totalAmount") or 0),
                   sum(float(f.get("amount") or 0) for f in (b.get("financeCharges") or [])))
                  for b in bills if b.get("billClosingDate") and b.get("dueDate")))
    if len(cic) < 2:
        return
    cic = cic[-5:]
    pagamentos = {round(float(p.get("amount") or 0), 2) for b in bills for p in (b.get("payments") or [])}
    pagamentos |= {round(c[2], 2) for c in cic}
    ini = (date.fromisoformat(cic[0][0]) - timedelta(days=10)).isoformat()
    r = get("/v2/transactions", h, {"accountId": ac["id"], "dateFrom": ini, "dateTo": date.today().isoformat()})
    if "_http" in r:
        print(f"  reconciliacao: /v2/transactions falhou: {r}")
        return
    txs = r.get("results", [])
    print("\n  >> RECONCILIACAO DAS FATURAS FECHADAS (totais, sem descricoes)")
    print("  fechamento | venc.      | total banco | compras Pluggy | falta   | encargos | parcelas 2+ na Pluggy")
    for i in range(1, len(cic)):
        ini_c, fim_c = cic[i - 1][0], cic[i][0]
        soma, n_parc, v_parc = 0.0, 0, 0.0
        for t in txs:
            d = (t.get("date") or "")[:10]
            if not (ini_c < d <= fim_c):
                continue
            tipo = (t.get("type") or "").upper()
            valor = _brl(t)
            if tipo == "CREDIT":
                if round(valor, 2) in pagamentos:
                    continue          # pagamento da fatura, nao e compra
                soma -= valor         # estorno
                continue
            soma += valor
            meta = t.get("creditCardMetadata") or {}
            if (meta.get("installmentNumber") or 1) > 1:
                n_parc, v_parc = n_parc + 1, v_parc + valor
        total = cic[i][2]
        print(f"  {fim_c} | {cic[i][1]} | {total:>11.2f} | {soma:>14.2f} | {total - soma:>7.2f} | {cic[i][3]:>8.2f} | {n_parc} ({v_parc:.2f})")
    print("  -> se 'falta' for parecida todo mes (e maior que os encargos), sao parcelas/itens que a Pluggy nao manda como compra.")

    # Calibracao: o fechamento informado pela Pluggy pode estar alguns dias depois do real.
    print("\n  >> CALIBRACAO DO DIA DE FECHAMENTO (falta por fatura, voltando N dias o fechamento)")
    print("  recuo | " + " | ".join(f"venc {cic[i][1]}" for i in range(1, len(cic))) + " | soma |falta|")
    for recuo in range(0, 7):
        faltas = []
        for i in range(1, len(cic)):
            ini_c = (date.fromisoformat(cic[i - 1][0]) - timedelta(days=recuo)).isoformat()
            fim_c = (date.fromisoformat(cic[i][0]) - timedelta(days=recuo)).isoformat()
            soma = 0.0
            for t in txs:
                d = (t.get("date") or "")[:10]
                if not (ini_c < d <= fim_c):
                    continue
                valor = _brl(t)
                if (t.get("type") or "").upper() == "CREDIT":
                    if round(valor, 2) in pagamentos:
                        continue
                    soma -= valor
                else:
                    soma += valor
            faltas.append(cic[i][2] - soma)
        print(f"  {recuo:>5} | " + " | ".join(f"{f:>15.2f}" for f in faltas) + f" | {sum(abs(f) for f in faltas):>8.2f}")
    print("  -> o recuo com menor soma de |falta| e o fechamento real; tarifas de 98,00 por fatura ficam como residuo.")

    # Hipotese: o C6 fecha num dia fixo do mes (recuando p/ dia util se cair no fim de semana).
    # Pagamento a mais numa fatura (ex.: 758,66 em 18/08) vira credito na fatura seguinte.
    def _soma_pagtos(b):
        return sum(float(p.get("amount") or 0) for p in (b.get("payments") or []))
    por_venc = {b["dueDate"][:10]: b for b in bills if b.get("dueDate")}
    def _fecha(venc_iso, dia, util):
        v = date.fromisoformat(venc_iso)
        m = v.month - 1 or 12
        a = v.year - (1 if v.month == 1 else 0)
        d = date(a, m, dia)
        while util and d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    print("\n  >> HIPOTESE: fechamento em dia fixo (falta ja descontando o credito de pagamento a mais)")
    print("  dia | fds->dia util | " + " | ".join(f"venc {cic[i][1]}" for i in range(1, len(cic))) + " | soma |falta|")
    for util in (True, False):
        for dia in range(20, 27):
            faltas = []
            for i in range(1, len(cic)):
                ini_c = _fecha(cic[i - 1][1], dia, util).isoformat()
                fim_c = _fecha(cic[i][1], dia, util).isoformat()
                soma = 0.0
                for t in txs:
                    d = (t.get("date") or "")[:10]
                    if not (ini_c < d <= fim_c):
                        continue
                    valor = _brl(t)
                    if (t.get("type") or "").upper() == "CREDIT":
                        if round(valor, 2) in pagamentos:
                            continue
                        soma -= valor
                    else:
                        soma += valor
                ant = por_venc.get(cic[i - 1][1])
                extra = max(0.0, _soma_pagtos(ant) - float(ant.get("totalAmount") or 0)) if ant else 0.0
                faltas.append(cic[i][2] - soma + extra)
            print(f"  {dia:>3} | {'sim' if util else 'nao':^13} | " + " | ".join(f"{f:>15.2f}" for f in faltas) + f" | {sum(abs(f) for f in faltas):>8.2f}")
    print("  -> a linha com soma proxima de 0 (so tarifas/IOF de residuo) e o dia real de fechamento.")


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
            if ac.get("type") == "CREDIT":
                analisar_cartao(ac, bills, h)
            tx = get("/v2/transactions", h, {"accountId": ac["id"],
                                             "dateFrom": (date.today() - timedelta(days=30)).isoformat(),
                                             "dateTo": date.today().isoformat()})
            print(f"  /v2/transactions respondeu: {'erro ' + str(tx.get('_http')) if '_http' in tx else 'ok'}")


if __name__ == "__main__":
    main()
