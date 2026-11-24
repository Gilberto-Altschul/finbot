"""
Sugere limites por categoria a partir do histórico de gastos, seguindo a regra 50/30/20 (só leitura: não grava e não envia nada).

    python -m app.diag_limites --user-key 0373fa8f1a099c9c [--renda 8000] [--meses 3] [--fixas Moradia,Saúde]

- Renda: média mensal das receitas dos últimos meses (só os meses em que houve receita). Sem receita registrada, informe --renda.
- Gasto: média por categoria dos últimos meses completos (só os meses em que a categoria teve gasto), como as sugestões do painel.
- Necessidades ficam com 50% da renda e Desejos com 30%. Se o histórico do grupo cabe na meta, o limite é a própria média
  (arredondada a R$ 10). Se passa da meta, os limites são reduzidos na mesma proporção; categorias de --fixas (aluguel, plano
  de saúde...) mantêm a média e só as demais cedem. "Dívidas e Financiamentos" não recebe limite: é compromisso e entra nos 20%.
- Mostra também os comandos "limite <categoria> <valor>" para colar no WhatsApp.

Rode na pasta do projeto, com o .env e o .venv ativos.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MINIMO = 30.0   # categorias com média menor que isso não ganham limite (ruído)


def brl(v):
    return f"R$ {float(v):>10,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def arred10(v):
    return int(round(v / 10.0)) * 10


def sugerir(renda, medias, grupo_de, fixas=()):
    """medias: {chave: média mensal}; grupo_de: {chave: 'nec'|'des'|'pou'}; fixas: chaves que não encolhem.
    Devolve {chave: {'grupo', 'media', 'limite'}} para 'nec' e 'des'."""
    metas = {"nec": 0.50 * renda, "des": 0.30 * renda}
    saida = {}
    for g, meta in metas.items():
        cats = {k: v for k, v in medias.items() if grupo_de.get(k, "des") == g and v >= MINIMO}
        total = sum(cats.values())
        fixo = sum(v for k, v in cats.items() if k in fixas)
        livre = total - fixo
        if total <= meta or livre <= 0:
            fator = 1.0
        else:
            fator = max(0.0, (meta - fixo) / livre)
        for k, v in cats.items():
            lim = v if (fator == 1.0 or k in fixas) else v * fator
            saida[k] = {"grupo": g, "media": round(v, 2), "limite": arred10(lim)}
    return saida


def main(chave, renda_manual, n_meses, fixas_txt):
    import app.database as db
    from app.diag_resumo import achar_telefone
    from app.painel import _REGRA_PADRAO, _mes_somado, _limites_mes
    from app.utils import _normalize
    from datetime import date

    fone = achar_telefone(chave)
    if not fone:
        sys.exit("Não achei telefone com essa chave.")
    hoje = date.today()
    atual = hoje.strftime("%Y-%m")
    meses = [_mes_somado(atual, -i) for i in range(n_meses, 0, -1)]
    print(f"Usuário …{fone[-4:]} | meses analisados: {', '.join(meses)}\n")

    # Renda
    rendas = [db.monthly_income_total(fone, m) for m in meses]
    com_renda = [r for r in rendas if r > 0]
    if renda_manual:
        renda = renda_manual
        print(f"Renda mensal (informada): {brl(renda)}")
    elif com_renda:
        renda = sum(com_renda) / len(com_renda)
        print(f"Renda mensal (média de {len(com_renda)} mês(es) com receita): {brl(renda)}   por mês: {[round(r, 2) for r in rendas]}")
    else:
        sys.exit("Sem receita registrada nesses meses. Informe a renda com --renda 8000.\n"
                 "Se achar que deveria haver receita, rode de novo: uma queda de conexão com o banco aparece como receita zero.")

    # Gasto por categoria e mês
    linhas = db.linhas_gastos_periodo(fone, _limites_mes(meses[0])[0].isoformat(), _limites_mes(meses[-1])[1].isoformat())
    por_mes, nomes = {}, {}
    for r in linhas:
        m = str(r.get("billing_date") or "")[:7]
        bruta = (r.get("category") or "").strip() or "Outros"
        k = _normalize(bruta)
        nomes.setdefault(k, bruta)
        por_mes.setdefault(k, {}).setdefault(m, 0.0)
        por_mes[k][m] += float(r["amount"])
    medias = {k: sum(v for v in d.values() if v > 0) / max(1, sum(1 for v in d.values() if v > 0)) for k, d in por_mes.items()}
    ajustes = db.regra_grupos(fone)
    grupo_de = {k: (ajustes.get(k) or _REGRA_PADRAO.get(k, "des")) for k in medias}
    fixas = {_normalize(x) for x in (fixas_txt or "").split(",") if x.strip()}

    sug = sugerir(renda, medias, grupo_de, fixas)
    rotulo = {"nec": "NECESSIDADES (meta 50%)", "des": "DESEJOS (meta 30%)"}
    for g in ("nec", "des"):
        meta = (0.5 if g == "nec" else 0.3) * renda
        itens = sorted((x for x in sug.items() if x[1]["grupo"] == g), key=lambda x: -x[1]["media"])
        media_g = sum(x[1]["media"] for x in itens)
        print(f"\n=== {rotulo[g]}: {brl(meta)} | histórico {brl(media_g)} ({'cabe' if media_g <= meta else 'passa ' + brl(media_g - meta)}) ===")
        for k, x in itens:
            marca = "  (fixa)" if k in fixas else ("  ↓ reduzido" if x["limite"] < arred10(x["media"]) else "")
            print(f"  {nomes[k]:28} média {brl(x['media'])}  → limite {brl(x['limite'])}{marca}")
    dividas = sum(v for k, v in medias.items() if grupo_de.get(k) == "pou")
    gasto_limites = sum(x["limite"] for x in sug.values())
    sobra = renda - gasto_limites - dividas
    print(f"\n=== POUPANÇA E DÍVIDAS (meta 20% = {brl(0.2 * renda)}) ===")
    print(f"  Dívidas e financiamentos (média): {brl(dividas)}")
    print(f"  Sobra se gastar exatamente os limites: {brl(sobra)}  ({sobra / renda * 100:.0f}% da renda)")

    print("\n=== PARA COLAR NO WHATSAPP (um por mensagem) ===")
    for k, x in sorted(sug.items(), key=lambda x: -x[1]["limite"]):
        if x["limite"] > 0:
            print(f"  limite {nomes[k].lower()} {x['limite']}")


if __name__ == "__main__":
    a = sys.argv

    def opc(n):
        return a[a.index(n) + 1] if n in a else None
    if "--user-key" not in a:
        sys.exit("uso: python -m app.diag_limites --user-key <16 caracteres> [--renda 8000] [--meses 3] [--fixas Moradia,Saúde]")
    renda = float(opc("--renda").replace(",", ".")) if opc("--renda") else None
    main(opc("--user-key"), renda, max(1, min(int(opc("--meses") or 3), 12)), opc("--fixas"))
