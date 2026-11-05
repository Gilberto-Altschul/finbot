"""
Atualiza por fora (sem WhatsApp) as contas, saldos e faturas de UM usuário, como o cron faz.
Grava só em finbot_contas, finbot_saldo_historico e finbot_faturas. Não envia mensagem e não toca nos lançamentos.

    python atualizar_usuario.py --user-key 0373fa8f1a099c9c              # só mostra o que há (não grava)
    python atualizar_usuario.py --user-key 0373fa8f1a099c9c --executar   # busca na Pluggy e grava

Rode na pasta do projeto, com o .env (Supabase, Pluggy e ENCRYPTION_KEY) e o .venv ativo.
"""
import asyncio
import sys


def achar_telefone(chave: str):
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


def mostrar(fone: str, titulo: str) -> None:
    import app.dados_banco as dbk
    print(f"\n--- {titulo} ---")
    contas = dbk.ler_contas(fone)
    for c in contas:
        print(f"{c['tipo']:6} {str(c['nome'])[:22]:22} saldo {c['saldo']}  sincronizado_em {c['sincronizado_em']}")
    for c in (c for c in contas if c["tipo"] == "CREDIT"):
        print(f"\nFaturas de {c['nome']}:")
        for ci, st in dbk.ler_faturas(fone, c["id"])[-4:]:
            print(f"  vence {ci.vencimento}  fecha {ci.fechamento}  total {ci.total}  {st}")


async def main(chave: str, executar: bool) -> None:
    from app.pluggy_service import PluggyService
    import app.dados_banco as dbk
    fone = achar_telefone(chave)
    if not fone:
        sys.exit("Não achei nenhum telefone com essa chave em finbot_user_connections.")
    print(f"Usuário encontrado: …{fone[-4:]}")
    mostrar(fone, "antes")
    if not executar:
        print("\n(nada foi gravado; use --executar para atualizar)")
        return
    pluggy = PluggyService()
    n = await dbk.atualizar(lambda: pluggy, fone)
    print(f"\natualizar() gravou {n} conta(s).")
    mostrar(fone, "depois")


if __name__ == "__main__":
    a = sys.argv
    if "--user-key" not in a:
        sys.exit("uso: python atualizar_usuario.py --user-key <16 caracteres> [--executar]")
    asyncio.run(main(a[a.index("--user-key") + 1], "--executar" in a))
