"""
Diagnóstico: a Pluggy devolve o código de barras do boleto nas transações da conta? Dá para tirar o vencimento dele?

Uso (na raiz do projeto, com o .venv ativo e o .env com as chaves da Pluggy):
    python diag_boleto_pluggy.py prevent --user-key 890a772cf33d0238
    python diag_boleto_pluggy.py prevent --user-key 890a772cf33d0238 --desde 2026-06-01

Só LÊ da Pluggy. Não grava nada, não mexe no Supabase. Não imprime nomes de pagador/recebedor nem o código de barras:
mostra só se o campo veio preenchido e o vencimento calculado.
"""
import asyncio
import sys
from datetime import date, timedelta

import requests

BASE_FATOR = date(2025, 2, 22)   # fator 1000 = 22/02/2025 (o fator reiniciou em 2025; antes: 1000 = 03/07/2000)


def vencimento_do_codigo(codigo: str | None, perto_de: date) -> date | None:
    """Vencimento a partir do fator de 4 dígitos. Boleto bancário: código de barras (44 dígitos, posições 6-9) ou
    linha digitável (47 dígitos, posições 34-37). Boleto de convênio (começa com 8) não tem esse fator. O fator
    repete a cada 9.000 dias: escolhe a data mais perto da data do pagamento."""
    d = "".join(c for c in (codigo or "") if c.isdigit())
    if len(d) == 44 and d[0] != "8":
        fator = int(d[5:9])
    elif len(d) == 47 and d[0] != "8":
        fator = int(d[33:37])
    else:
        return None
    if fator == 0:
        return None   # boleto sem vencimento
    candidatos = [BASE_FATOR + timedelta(days=(fator - 1000) + 9000 * k) for k in (-1, 0, 1)]
    return min(candidatos, key=lambda x: abs((x - perto_de).days))


def _teste_interno() -> None:
    # fator de 30/10/2026 e de 30/09/2026, em um código de barras de 44 dígitos fictício
    for alvo in (date(2026, 10, 30), date(2026, 9, 30)):
        fator = 1000 + (alvo - BASE_FATOR).days
        barras = "3419" + "1" + f"{fator:04d}" + "0000012345" + "0" * 25
        assert len(barras) == 44, len(barras)
        assert vencimento_do_codigo(barras, alvo + timedelta(days=1)) == alvo, alvo
    assert vencimento_do_codigo("8" + "0" * 43, date(2026, 10, 30)) is None   # convênio
    assert vencimento_do_codigo(None, date(2026, 10, 30)) is None


async def main(filtro: str, desde: str, user_key: str) -> None:
    import app.database as db
    from app.pluggy_service import PluggyService
    svc = PluggyService()
    # Como o app faz: as contas vêm do banco (finbot_contas), não de uma listagem geral de itens na Pluggy.
    contas = [c for c in (db.get_db().table("finbot_contas").select("account_id, item_id, tipo, nome, banco_nome")
                          .eq("user_key", user_key).execute().data or [])
              if (c.get("tipo") or "").upper() != "CREDIT"]
    if not contas:
        sys.exit(f"Nenhuma conta que não seja de cartão em finbot_contas para user_key={user_key}. Confira a chave.")
    print(f"{len(contas)} conta(s) analisada(s): " + ", ".join(f"{c.get('banco_nome')} / {c.get('nome')}" for c in contas) + "\n")
    achou = 0
    for conta in contas:
        # Mesmo endereço e parâmetros que o app usa em pluggy_service.sync_user_transactions (/transactions foi desativado: 410).
        resp = requests.get(f"{svc.base_url}/v2/transactions", headers=svc.headers, timeout=30,
                            params={"accountId": conta["account_id"], "dateFrom": desde,
                                    "dateTo": date.today().isoformat()})
        resp.raise_for_status()
        for tx in resp.json().get("results", []):
            texto = f"{tx.get('description') or ''} {tx.get('descriptionRaw') or ''}".lower()
            if filtro.lower() not in texto:
                continue
            achou += 1
            pd = tx.get("paymentData") or {}
            bm = pd.get("boletoMetadata") or {}
            dia = date.fromisoformat(str(tx["date"])[:10])
            venc = vencimento_do_codigo(bm.get("barcode"), dia) or vencimento_do_codigo(bm.get("digitableLine"), dia)
            print(f"{dia}  R$ {tx.get('amount')}  {str(tx.get('description'))[:40]}")
            print(f"   conta: {conta.get('banco_nome')} / {conta.get('nome')}")
            print(f"   paymentData: {'sim' if pd else 'NÃO'} | paymentMethod: {pd.get('paymentMethod')} "
                  f"| operationType: {tx.get('operationType')}")
            print(f"   boletoMetadata: {'sim' if bm else 'NÃO'} | barcode: {'sim' if bm.get('barcode') else 'não'} "
                  f"| linha digitável: {'sim' if bm.get('digitableLine') else 'não'}")
            print(f"   vencimento calculado: {venc or '— (não deu para calcular)'}\n")
    if not achou:
        print(f"Nenhuma transação com '{filtro}' nessas contas desde {desde}.")


if __name__ == "__main__":
    _teste_interno()
    if len(sys.argv) < 2:
        sys.exit("uso: python diag_boleto_pluggy.py <texto da descrição> --user-key <16 caracteres> [--desde AAAA-MM-DD]")
    if "--user-key" not in sys.argv:
        sys.exit("Faltou --user-key (prefixo de 16 caracteres do user_phone, o mesmo usado em finbot_reclassify_movement).")
    desde = sys.argv[sys.argv.index("--desde") + 1] if "--desde" in sys.argv else "2026-08-01"
    asyncio.run(main(sys.argv[1], desde, sys.argv[sys.argv.index("--user-key") + 1]))
