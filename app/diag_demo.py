"""
Lista os usuários de demonstração que existem no banco (SÓ LEITURA: não grava, não apaga, não envia mensagem).

    python -m app.diag_demo

Mostra, por usuário demo: chave (10 caracteres), final do telefone (4 últimos dígitos), nº de lançamentos e data do último
lançamento fictício. A chave da sua própria demo: python -c "from app.utils import get_lookup_prefix as g; print(g('whatsapp:+0'+'<seu número só com dígitos>')[:10])"

Rode na pasta do projeto, com o .env e o .venv ativos.
"""
import re


def main():
    import app.database as db
    from app.utils import descriptografar_telefone

    cli = db.get_db()
    por: dict[str, dict] = {}
    ini = 0
    while True:
        lote = (cli.table("finbot_expenses").select("user_phone, pluggy_transaction_id, purchase_date")
                .like("pluggy_transaction_id", "demo-%").range(ini, ini + 999).execute().data or [])
        for r in lote:
            chave = str(r["pluggy_transaction_id"])[5:15]
            d = por.setdefault(chave, {"n": 0, "fone": None, "ultimo": ""})
            d["n"] += 1
            d["ultimo"] = max(d["ultimo"], str(r["purchase_date"])[:10])
            if d["fone"] is None:
                try:
                    d["fone"] = descriptografar_telefone(r["user_phone"])
                except Exception:
                    d["fone"] = ""
        if len(lote) < 1000:
            break
        ini += 1000

    if not por:
        print("Nenhum dado de demonstração no banco.")
        return
    print(f"{'chave':12} {'telefone':10} {'lançamentos':>11}  último lançamento")
    for chave, d in sorted(por.items(), key=lambda kv: -kv[1]["n"]):
        digitos = re.sub(r"\D", "", d["fone"] or "")
        print(f"{chave:12} {('…' + digitos[-4:]) if digitos else '?':10} {d['n']:>11}  {d['ultimo']}")


if __name__ == "__main__":
    main()
