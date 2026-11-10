"""
Mostra o Resumo do mês de um usuário exatamente como o app monta para o WhatsApp (só leitura; não grava e não envia mensagem).

    python diag_resumo.py --user-key 0373fa8f1a099c9c [--mes 2026-11]
    python diag_resumo.py --user-key 0373fa8f1a099c9c --categoria Moradia [--sub "Condomínio"] [--mes 2026-11]

Com --categoria mostra o que o usuário vê ao pedir uma categoria de um mês (ex.: "gastos moradia 2026-11"), mais as linhas brutas
(data da compra, competência, valor) para conferir de onde sai cada número.

Rode na pasta do projeto, com o .env e o .venv ativo.
"""
import asyncio
import sys


def achar_telefone(chave):
    import app.database as db
    from app.utils import descriptografar_telefone, get_lookup_prefix
    vistos = set()
    for r in db.get_db().table("finbot_user_connections").select("user_phone").execute().data or []:
        try:
            fone = descriptografar_telefone(r["user_phone"])
        except Exception:
            continue
        if fone in vistos:
            continue
        vistos.add(fone)
        if get_lookup_prefix(fone) == chave:
            return fone
    return None


async def main(chave, mes, categoria=None, sub=None):
    import app.tools as tools
    fone = achar_telefone(chave)
    if not fone:
        sys.exit("Não achei telefone com essa chave.")
    print(f"Usuário …{fone[-4:]}\n")
    args = {"mes": mes} if mes else {}
    if categoria:
        args["categoria"] = categoria
        if sub:
            args["subcategoria"] = sub
        res = await tools.execute("listar_categoria", args, fone)
        print("=== TEXTO DO WHATSAPP ===")
        print(res.get("mensagem") or res)
        print("\n=== LINHAS BRUTAS ===")
        for t in sorted(res.get("transacoes") or [], key=lambda x: str(x.get("purchase_date")), reverse=True):
            print(f"compra {str(t.get('purchase_date'))[:10]} | competência {str(t.get('billing_date'))[:10]} | {float(t['amount']):>9.2f} | {str(t.get('subcategory')):28} | {t.get('description')}")
        return
    res = await tools.execute("resumo_mensal", args, fone)
    print("=== TEXTO DO WHATSAPP ===")
    print(res.get("mensagem") or res)
    print("\n=== NÚMEROS ===")
    for k in ("total_receitas", "total_gastos", "gastos_realizados", "previstas", "saldo"):
        if k in res:
            print(f"{k}: {res[k]}")


if __name__ == "__main__":
    a = sys.argv
    if "--user-key" not in a:
        sys.exit("uso: python diag_resumo.py --user-key <16 caracteres> [--mes 2026-11] [--categoria X [--sub Y]]")
    def opc(n):
        return a[a.index(n) + 1] if n in a else None
    asyncio.run(main(opc("--user-key"), opc("--mes"), opc("--categoria"), opc("--sub")))
