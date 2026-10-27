"""
Previsão de fechamento do mês, por categoria.

Funções puras (sem banco): quem lê os dados é app.dados_banco.previsao_mes.
O "mês" segue a regra do resto do FinBot (mês de COBRANÇA): cartão conta no mês da fatura, débito/Pix no dia.
previsto = realizado até hoje + o que ainda deve sair até o fim do mês (média diária por categoria dos gastos de
conta + contas fixas que ainda vão vencer + parcelas previstas do cartão).
"""
from __future__ import annotations

from datetime import date, timedelta

from app import fluxo, utils

_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro"]
LIMITE_ATENCAO = 0.9  # a partir de 90% do orçamento já avisa


def ritmo_por_categoria(despesas: list[dict], excluir: set[str], hoje: date, janela: int = fluxo.JANELA_RITMO
                        ) -> dict[str, float]:
    """Média diária por categoria dos gastos variáveis de conta nos últimos `janela` dias (sem contas fixas
    e sem gastos únicos acima do teto)."""
    ini = hoje - timedelta(days=janela)
    soma: dict[str, float] = {}
    for r in despesas:
        dia = fluxo._dia(r)
        if dia is None or not (ini <= dia <= hoje) or fluxo._chave(r.get("description") or "") in excluir:
            continue
        try:
            v = abs(float(r["amount"]))
        except (TypeError, ValueError, KeyError):
            continue
        if v <= fluxo.TETO_EXCEPCIONAL:
            cat = (r.get("category") or "Outros").strip() or "Outros"
            soma[cat] = soma.get(cat, 0.0) + v
    return {c: round(v / janela, 4) for c, v in soma.items()}


def categoria_da_conta_fixa(despesas: list[dict], descricao_evento: str) -> str:
    """Categoria da linha mais recente cuja descrição bate com a do evento ('<desc> (conta fixa)')."""
    chave = fluxo._chave(descricao_evento.rsplit(" (", 1)[0])
    achadas = sorted((r for r in despesas if fluxo._chave(r.get("description") or "") == chave),
                     key=lambda r: str(r.get("purchase_date") or r.get("billing_date")))
    return ((achadas[-1].get("category") if achadas else None) or "Outros").strip() or "Outros"


def montar(realizado: dict[str, float], restante: dict[str, float], limites: dict[str, float]) -> list[dict]:
    """Une as três fontes por categoria (sem diferenciar maiúsculas/acentos) e marca o status contra o orçamento."""
    linhas: dict[str, dict] = {}

    def linha(nome: str) -> dict:
        return linhas.setdefault(utils._normalize(nome), {"categoria": nome, "realizado": 0.0, "restante": 0.0, "limite": None})

    # O nome de exibição é o de quem tem gasto (o do orçamento só entra se a categoria ainda não tem gasto).
    for nome, v in realizado.items():
        linha(nome)["realizado"] += float(v)
    for nome, v in restante.items():
        linha(nome)["restante"] += float(v)
    for nome, v in limites.items():
        linha(nome)["limite"] = float(v)
    saida = []
    for x in linhas.values():
        x["realizado"], x["restante"] = round(x["realizado"], 2), round(x["restante"], 2)
        x["previsto"] = round(x["realizado"] + x["restante"], 2)
        lim = x["limite"]
        x["status"] = ("estoura" if lim and x["previsto"] > lim else
                       "atencao" if lim and x["previsto"] > lim * LIMITE_ATENCAO else "ok")
        if x["previsto"] > 0 or lim:
            saida.append(x)
    return sorted(saida, key=lambda x: -x["previsto"])


def formatar(linhas: list[dict], hoje: date, fim_mes: date, mes_anterior: float | None = None, top: int = 10) -> str:
    if not linhas:
        return "📊 Ainda não tenho gastos suficientes neste mês para prever o fechamento."
    brl = fluxo.brl
    real = round(sum(x["realizado"] for x in linhas), 2)
    prev = round(sum(x["previsto"] for x in linhas), 2)
    restam = (fim_mes - hoje).days
    out = [f"📊 *Previsão do mês — {_MESES[hoje.month - 1].capitalize()}/{hoje.year}*",
           f"\n💸 Gasto até agora: *{brl(real)}*",
           f"🔮 Previsto para o fim do mês: *{brl(prev)}* _(faltam {restam} dia{'s' if restam != 1 else ''})_"]
    if mes_anterior:
        dif = round(prev - mes_anterior, 2)
        out.append(f"↔️ Mês passado: {brl(mes_anterior)} → {'+' if dif >= 0 else '-'}{brl(abs(dif))} "
                   f"({'+' if dif >= 0 else '-'}{abs(round(dif / mes_anterior * 100))}%)")
    out.append("\n*Por categoria*")
    for x in linhas[:top]:
        txt = f"• {x['categoria']}: *{brl(x['previsto'])}*"
        if x["restante"] > 0.005:
            txt += f" _(até agora {brl(x['realizado'])})_" if x["realizado"] > 0.005 else " _(ainda por vir)_"
        if x["limite"]:
            if x["status"] == "estoura":
                txt += f" · limite {brl(x['limite'])} ⚠️ passa {brl(x['previsto'] - x['limite'])}"
            elif x["status"] == "atencao":
                txt += f" · limite {brl(x['limite'])} 🟡 perto"
            else:
                txt += f" · limite {brl(x['limite'])} ✅"
        out.append(txt)
    if len(linhas) > top:
        resto = round(sum(x["previsto"] for x in linhas[top:]), 2)
        out.append(f"• Outras {len(linhas) - top} categorias: {brl(resto)}")
    estouros = [x for x in linhas if x["status"] == "estoura"]
    if estouros:
        out.append(f"\n⚠️ {len(estouros)} categoria{'s' if len(estouros) > 1 else ''} deve{'m' if len(estouros) > 1 else ''} passar do orçamento.")
    out.append("_Estimativa: realizado + média diária dos gastos de débito/Pix (últimos 60 dias) por categoria + "
               "contas fixas que ainda vão vencer + parcelas previstas do cartão. Compras novas no cartão caem na fatura "
               "do mês seguinte e não entram aqui._")
    return "\n".join(out)
