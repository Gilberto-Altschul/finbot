"""
Mostra o que o painel exibe para um usuário, com os mesmos números do servidor (só leitura: não grava e não envia mensagem).

    python -m app.diag_painel --user-key 0373fa8f1a099c9c [--mes 2026-11] [--fluxo]

Mostra o Resumo (receitas, gastos, saldo, categorias com limite, regra 50/30/20 e a série mensal do gráfico).
Com --fluxo mostra também as entradas e contas previstas do Fluxo e os itens marcados como "não vai se repetir".

Rode na pasta do projeto (a que tem a pasta app), com o .env e o .venv ativos.
A chave do usuário são os 16 caracteres que o diag_resumo.py também usa.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def brl(v):
    return "-" if v is None else f"R$ {float(v):>10,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def main(chave, mes, com_fluxo):
    import app.painel as painel
    from app.diag_resumo import achar_telefone
    fone = achar_telefone(chave)
    if not fone:
        sys.exit("Não achei telefone com essa chave.")
    d = painel.dados_resumo(fone, mes=mes)
    print(f"Usuário …{fone[-4:]} | mês {d['mes']} | demonstração: {d['demonstracao']}\n")
    print(f"Receitas {brl(d['receitas'])}   Gastos {brl(d['gastos'])}   Saldo {brl(d['saldo'])}   (parcelas previstas {brl(d['previstas'])})")
    c = d["comparacao"]
    print(f"Comparação: {brl(c['atual'])} contra {brl(c['anterior'])} ({c['referencia']}), diferença {brl(c['dif'])}\n")

    print("=== POR BANCO ===")
    for x in d["por_banco"]:
        print(f"  {x['nome']:28} {brl(x['gasto'])}")

    print("\n=== CATEGORIAS (gasto / limite) ===")
    for x in d["categorias"]:
        lim = brl(x["orcamento"]) if x["orcamento"] else "sem limite"
        pct = f"{x['pct']}% {x['status']}" if x["pct"] is not None else ""
        print(f"  {x['nome']:26} {brl(x['gasto'])} / {lim}  {pct}")

    r = d.get("regra")
    print("\n=== REGRA 50/30/20 ===")
    if not r:
        print("  (sem receitas no mês)")
    else:
        for g in r["grupos"]:
            print(f"  {g['nome']:22} {brl(g['valor'])}  {g['pct']:>4}% da renda (meta {g['meta']}%)  {g['situacao']}")
            for x in g["cats"]:
                print(f"      {x['nome']:24} {brl(x['gasto'])}")
        print(f"  Faltam {brl(r['falta'])} para 20% | excesso necessidades {brl(r['excesso_nec'])} | excesso desejos {brl(r['excesso_des'])}")

    print("\n=== SÉRIE DO GRÁFICO ===")
    for s in d["serie"]:
        print("  ", {k: v for k, v in s.items()})

    if com_fluxo:
        import app.dados_banco as dados_banco
        import app.fluxo as fluxo
        import app.database as db
        print("\n=== FLUXO ===")
        p = fluxo.para_painel(dados_banco.fluxo_projecao(fone, 30), dados_banco.hoje_local())
        if p.get("vazio"):
            print("  (sem conta corrente com saldo)")
        else:
            print(f"  Saldo hoje {brl(p['saldo_inicial'])} -> {brl(p['saldo_final'])} em {p['fim']} | ritmo {brl(p['ritmo'])}/dia")
            for dia in p["dias"]:
                for e in dia["eventos"]:
                    print(f"  {dia['data']}  {e['tipo']:8} {brl(e['valor'])}  {e['nome']}   [{e['chave']}]")
        ign = db.fluxo_ignorados(fone)
        print("\n  Itens marcados como 'não vai se repetir':", ign or "nenhum")


if __name__ == "__main__":
    a = sys.argv
    if "--user-key" not in a:
        sys.exit("uso: python -m app.diag_painel --user-key <16 caracteres> [--mes 2026-11] [--fluxo]")
    def opc(n):
        return a[a.index(n) + 1] if n in a else None
    main(opc("--user-key"), opc("--mes"), "--fluxo" in a)
