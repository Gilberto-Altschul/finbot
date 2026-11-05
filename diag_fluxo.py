"""
Mostra o fluxo de caixa de um usuário exatamente como o app calcula (só leitura; não grava, não envia WhatsApp).

    python diag_fluxo.py --user-key 0373fa8f1a099c9c [--dias 30]

Imprime: o texto que iria ao WhatsApp e, abaixo, a linha do tempo bruta (data, evento, valor, saldo).
Rode na pasta do projeto, com o .env e o .venv ativo.
"""
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


def main(chave, dias):
    import app.dados_banco as dbk
    import app.fluxo as fluxo_mod
    fone = achar_telefone(chave)
    if not fone:
        sys.exit("Não achei telefone com essa chave.")
    print(f"Usuário …{fone[-4:]} | hoje (Brasília): {dbk.hoje_local()} | horizonte: {dias} dias\n")
    proj = dbk.fluxo_projecao(fone, dias)
    if proj is None:
        print("Sem conta corrente com saldo: o app responderia 'sem contas'.")
        return
    print("=== TEXTO DO WHATSAPP ===")
    print(fluxo_mod.formatar(proj))
    print("\n=== LINHA DO TEMPO BRUTA ===")
    print(f"saldo inicial {proj['saldo_inicial']:.2f} | ritmo/dia {proj['ritmo']:.2f} ({proj['ritmo_texto']})")
    for dia, evs, saldo in proj["dias"]:
        for e in evs:
            print(f"{dia}  {e.valor:>10.2f}  {str(e.descricao)[:40]:40}  saldo {saldo:.2f}")
    print(f"\nmínimo {proj['minimo']:.2f} em {proj['data_minimo']} | 1º dia negativo: {proj['primeiro_negativo']} | final {proj['saldo_final']:.2f}")


if __name__ == "__main__":
    a = sys.argv
    if "--user-key" not in a:
        sys.exit("uso: python diag_fluxo.py --user-key <16 caracteres> [--dias 30]")
    main(a[a.index("--user-key") + 1], int(a[a.index("--dias") + 1]) if "--dias" in a else 30)
