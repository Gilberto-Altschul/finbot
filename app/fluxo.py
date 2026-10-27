"""
Fluxo de caixa projetado: saldo das contas correntes dia a dia, com as saídas (vencimento das faturas)
e as entradas recorrentes (salário etc.) que já são conhecidas.

Funções puras (sem banco): quem lê os dados é app.dados_banco.fluxo_caixa.
Convenção: valor > 0 = entra dinheiro; valor < 0 = sai.
"""
from __future__ import annotations

import statistics
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from app import utils

# Só entradas "de verdade" recorrentes entram na previsão; reembolso/transferência não se repetem.
SUBCATEGORIAS_RECORRENTES = {"salario", "salário", "freelance e bonus", "freelance e bônus"}
_TOLERANCIA = 0.15  # o valor pode variar até 15% em relação à mediana e ainda ser "o mesmo" recebimento


@dataclass(frozen=True)
class Evento:
    data: date
    descricao: str
    valor: float  # + entrada, − saída


def _soma_mes(d: date, n: int) -> date:
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return date(ano, mes, min(d.day, monthrange(ano, mes)[1]))


def entradas_recorrentes(receitas: list[dict], hoje: date, dias: int) -> list[Evento]:
    """Detecta salário etc. a partir do histórico: mesma descrição em 2+ meses diferentes, valor parecido.
    A próxima data é a última ocorrência + 1 mês (mesmo dia). Se já caiu neste mês, não repete.
    `receitas`: [{description, amount, subcategory, billing_date|purchase_date}]"""
    fim = hoje + timedelta(days=dias)
    grupos: dict[str, list[tuple[date, float, str]]] = {}
    for r in receitas:
        sub = utils._normalize(r.get("subcategory") or "")
        desc = (r.get("description") or "").strip()
        if sub not in {utils._normalize(s) for s in SUBCATEGORIAS_RECORRENTES}:
            continue
        if not desc or "pagamento recebido" in desc.lower():
            continue
        bruto = r.get("purchase_date") or r.get("billing_date")
        try:
            dia = date.fromisoformat(str(bruto)[:10])
            valor = abs(float(r["amount"]))
        except (TypeError, ValueError):
            continue
        grupos.setdefault(utils._normalize(desc)[:24], []).append((dia, valor, desc))
    saida: list[Evento] = []
    for itens in grupos.values():
        itens.sort()
        meses = {(d.year, d.month) for d, _v, _n in itens}
        if len(meses) < 2:
            continue
        mediana = statistics.median(v for _d, v, _n in itens)
        parecidos = [x for x in itens if mediana and abs(x[1] - mediana) <= mediana * _TOLERANCIA]
        if len({(d.year, d.month) for d, _v, _n in parecidos}) < 2:
            continue
        ultimo_dia, ultimo_valor, nome = parecidos[-1]
        prox = _soma_mes(ultimo_dia, 1)
        while prox < hoje:  # ficou para trás: anda mês a mês até a próxima data futura
            prox = _soma_mes(prox, 1)
        if hoje <= prox <= fim:
            saida.append(Evento(prox, f"{nome} (previsto)", round(ultimo_valor, 2)))
    return sorted(saida, key=lambda e: e.data)


def projetar(saldo_inicial: float, eventos: list[Evento], hoje: date, dias: int) -> dict:
    """Linha do tempo só com os dias que têm movimento. Retorna:
    {saldo_inicial, dias: [(data, [Evento], saldo_apos)], minimo, data_minimo, primeiro_negativo, falta, saldo_final}"""
    fim = hoje + timedelta(days=dias)
    por_dia: dict[date, list[Evento]] = {}
    for e in eventos:
        if hoje <= e.data <= fim:
            por_dia.setdefault(e.data, []).append(e)
    saldo = round(saldo_inicial, 2)
    linhas = []
    minimo, data_min, primeiro_neg = saldo, hoje, (hoje if saldo < 0 else None)
    for dia in sorted(por_dia):
        # Entradas do dia antes das saídas: o salário que cai no dia do vencimento paga a fatura.
        evs = sorted(por_dia[dia], key=lambda e: -e.valor)
        for e in evs:
            saldo = round(saldo + e.valor, 2)
        linhas.append((dia, evs, saldo))
        if saldo < minimo:
            minimo, data_min = saldo, dia
        if saldo < 0 and primeiro_neg is None:
            primeiro_neg = dia
    return {"saldo_inicial": round(saldo_inicial, 2), "dias": linhas, "minimo": minimo, "data_minimo": data_min,
            "primeiro_negativo": primeiro_neg, "falta": round(-minimo, 2) if minimo < 0 else 0.0,
            "saldo_final": saldo, "horizonte": dias}


def brl(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {s}" if v < 0 else f"R$ {s}"


def formatar(proj: dict | None, sem_contas: bool = False) -> str:
    if sem_contas or proj is None:
        return "📈 Não achei contas correntes conectadas para projetar o fluxo de caixa."
    linhas = [f"📈 *Fluxo de caixa — próximos {proj['horizonte']} dias*",
              f"\n🏦 Saldo hoje (contas correntes): *{brl(proj['saldo_inicial'])}*\n"]
    if not proj["dias"]:
        linhas.append("Nenhuma entrada ou vencimento conhecido nesse período.")
    for dia, evs, saldo in proj["dias"]:
        for e in evs:
            icone = "🟢" if e.valor > 0 else "💳"
            sinal = "+" if e.valor > 0 else "-"
            linhas.append(f"{icone} {dia.strftime('%d/%m')} · {e.descricao}: {sinal}{brl(abs(e.valor))}")
        linhas.append(f"      ↳ saldo: *{brl(saldo)}*{' ⚠️' if saldo < 0 else ''}")
    linhas.append("")
    if proj["primeiro_negativo"]:
        linhas.append(f"⚠️ *O saldo fica negativo em {proj['primeiro_negativo'].strftime('%d/%m')}.*")
        linhas.append(f"📉 Menor ponto: *{brl(proj['minimo'])}* em {proj['data_minimo'].strftime('%d/%m')}"
                      f" — para fechar o período no zero, precisa entrar *{brl(proj['falta'])}* até lá.")
    else:
        linhas.append(f"✅ O saldo não fica negativo no período. Menor ponto: *{brl(proj['minimo'])}* em {proj['data_minimo'].strftime('%d/%m')}.")
    linhas.append("_Considera o saldo atual, o vencimento das faturas (lançado + parcelas previstas) e entradas recorrentes "
                  "(salário etc.) detectadas no histórico. Não inclui gastos novos de débito/Pix nem compras futuras no cartão._")
    return "\n".join(linhas)
