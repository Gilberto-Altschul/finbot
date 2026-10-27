"""
Fluxo de caixa projetado: saldo das contas correntes dia a dia, com as saídas conhecidas (vencimento das
faturas, contas fixas recorrentes), as entradas recorrentes (salário, INSS...) e o ritmo médio de gastos
do dia a dia (débito/Pix/boleto).

Funções puras (sem banco): quem lê os dados é app.dados_banco.fluxo_caixa.
Convenção: valor > 0 = entra dinheiro; valor < 0 = sai.
"""
from __future__ import annotations

import math
import statistics
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from app import utils

# Entradas recorrentes confiáveis (vale para a categoria OU a subcategoria da linha). Transferência de terceiros
# (Pix de familiares) fica de fora de propósito: costuma ser eventual.
SUBCATEGORIAS_RECORRENTES = {"salario", "salário", "freelance e bonus", "freelance e bônus",
                             "aposentadoria e beneficios", "aposentadoria e benefícios", "aposentadoria", "beneficios inss"}
_TOLERANCIA = 0.15      # o valor pode variar até 15% em relação à mediana e ainda ser "o mesmo" lançamento
MIN_SAIDA_FIXA = 200.0  # conta fixa recorrente só vale a partir daqui (abaixo disso é padaria, não aluguel)
TETO_EXCEPCIONAL = 3000.0  # gasto único acima disso não entra na média diária (compra pontual)
JANELA_RITMO = 60       # dias de histórico usados na média diária


@dataclass(frozen=True)
class Evento:
    data: date
    descricao: str
    valor: float  # + entrada, − saída


def _soma_mes(d: date, n: int) -> date:
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return date(ano, mes, min(d.day, monthrange(ano, mes)[1]))


def _dia(r: dict) -> date | None:
    try:
        return date.fromisoformat(str(r.get("purchase_date") or r.get("billing_date"))[:10])
    except (TypeError, ValueError):
        return None


def _chave(desc: str) -> str:
    return utils._normalize(desc)[:24]


def _recorrentes(linhas: list[dict], hoje: date, dias: int, sinal: int, rotulo: str, minimo: float = 0.0
                 ) -> tuple[list[Evento], set[str]]:
    """Mesma descrição em 2+ meses diferentes, valor parecido. Próxima data = última ocorrência + 1 mês
    (mesmo dia). Retorna (eventos dentro do horizonte, chaves detectadas — mesmo as fora do horizonte)."""
    fim = hoje + timedelta(days=dias)
    grupos: dict[str, list[tuple[date, float, str]]] = {}
    for r in linhas:
        desc = (r.get("description") or "").strip()
        dia = _dia(r)
        try:
            valor = abs(float(r["amount"]))
        except (TypeError, ValueError, KeyError):
            continue
        if not desc or dia is None or valor < minimo or "pagamento recebido" in desc.lower():
            continue
        grupos.setdefault(_chave(desc), []).append((dia, valor, desc))
    eventos: list[Evento] = []
    chaves: set[str] = set()
    for chave, itens in grupos.items():
        itens.sort()
        mediana = statistics.median(v for _d, v, _n in itens)
        parecidos = [x for x in itens if mediana and abs(x[1] - mediana) <= mediana * _TOLERANCIA]
        if len({(d.year, d.month) for d, _v, _n in parecidos}) < 2:
            continue
        chaves.add(chave)
        ultimo_dia, ultimo_valor, nome = parecidos[-1]
        prox = _soma_mes(ultimo_dia, 1)
        while prox < hoje:  # ficou para trás: anda mês a mês até a próxima data futura
            prox = _soma_mes(prox, 1)
        if hoje <= prox <= fim:
            eventos.append(Evento(prox, f"{nome} ({rotulo})", round(sinal * ultimo_valor, 2)))
    return sorted(eventos, key=lambda e: e.data), chaves


def entradas_recorrentes(receitas: list[dict], hoje: date, dias: int) -> list[Evento]:
    """Salário, INSS etc. `receitas`: [{description, amount, category, subcategory, purchase_date|billing_date}]"""
    aceitas = {utils._normalize(x) for x in SUBCATEGORIAS_RECORRENTES}
    boas = [r for r in receitas
            if {utils._normalize(r.get("subcategory") or ""), utils._normalize(r.get("category") or "")} & aceitas]
    return _recorrentes(boas, hoje, dias, +1, "previsto")[0]


def saidas_recorrentes(despesas: list[dict], hoje: date, dias: int, minimo: float = MIN_SAIDA_FIXA
                       ) -> tuple[list[Evento], set[str]]:
    """Contas fixas pagas pela conta (aluguel, condomínio, internet...). Retorna (eventos, chaves para
    tirá-las da média diária)."""
    return _recorrentes(despesas, hoje, dias, -1, "conta fixa", minimo)


def ritmo_diario(despesas: list[dict], excluir: set[str], hoje: date, janela: int = JANELA_RITMO) -> float:
    """Média diária dos gastos variáveis (débito/Pix/boleto) nos últimos `janela` dias, sem as contas fixas
    já previstas e sem gastos únicos muito grandes."""
    ini = hoje - timedelta(days=janela)
    total = 0.0
    for r in despesas:
        dia = _dia(r)
        if dia is None or not (ini <= dia <= hoje) or _chave(r.get("description") or "") in excluir:
            continue
        try:
            v = abs(float(r["amount"]))
        except (TypeError, ValueError, KeyError):
            continue
        if v <= TETO_EXCEPCIONAL:
            total += v
    return round(total / janela, 2)


def projetar(saldo_inicial: float, eventos: list[Evento], hoje: date, dias: int, ritmo: float = 0.0) -> dict:
    """Linha do tempo só com os dias que têm movimento (o ritmo diário é descontado entre eles). Retorna:
    {saldo_inicial, dias: [(data, [Evento], saldo_apos)], minimo, data_minimo, primeiro_negativo, falta,
     saldo_final, fim, ritmo, horizonte}"""
    fim = hoje + timedelta(days=dias)
    por_dia: dict[date, list[Evento]] = {}
    for e in eventos:
        if hoje <= e.data <= fim:
            por_dia.setdefault(e.data, []).append(e)
    saldo = round(saldo_inicial, 2)
    anterior = hoje
    linhas = []
    minimo, data_min, primeiro_neg = saldo, hoje, (hoje if saldo < 0 else None)

    def avancar(ate: date) -> None:
        """Gasta o ritmo diário de `anterior` até `ate`; marca o 1º dia negativo se o saldo acabar no meio."""
        nonlocal saldo, minimo, data_min, primeiro_neg, anterior
        gap = (ate - anterior).days
        if ritmo > 0 and gap > 0:
            if primeiro_neg is None and saldo - ritmo * gap < 0:
                primeiro_neg = anterior + timedelta(days=max(1, math.floor(saldo / ritmo) + 1)) if saldo >= 0 else ate
            saldo = round(saldo - ritmo * gap, 2)
            if saldo < minimo:
                minimo, data_min = saldo, ate
        anterior = ate

    for dia in sorted(por_dia):
        avancar(dia)
        # Entradas do dia antes das saídas: o salário que cai no dia do vencimento paga a fatura.
        evs = sorted(por_dia[dia], key=lambda e: -e.valor)
        for e in evs:
            saldo = round(saldo + e.valor, 2)
        linhas.append((dia, evs, saldo))
        if saldo < minimo:
            minimo, data_min = saldo, dia
        if saldo < 0 and primeiro_neg is None:
            primeiro_neg = dia
    avancar(fim)
    return {"saldo_inicial": round(saldo_inicial, 2), "dias": linhas, "minimo": minimo, "data_minimo": data_min,
            "primeiro_negativo": primeiro_neg, "falta": round(-minimo, 2) if minimo < 0 else 0.0,
            "saldo_final": saldo, "fim": fim, "ritmo": ritmo, "horizonte": dias}


def brl(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {s}" if v < 0 else f"R$ {s}"


def formatar(proj: dict | None, sem_contas: bool = False) -> str:
    if sem_contas or proj is None:
        return "📈 Não achei contas correntes conectadas para projetar o fluxo de caixa."
    linhas = [f"📈 *Fluxo de caixa — próximos {proj['horizonte']} dias*",
              f"\n🏦 Saldo hoje (contas correntes): *{brl(proj['saldo_inicial'])}*"]
    if proj.get("ritmo"):
        linhas.append(f"🛒 Gastos do dia a dia (média dos últimos {JANELA_RITMO} dias): *{brl(proj['ritmo'])}/dia*")
    linhas.append("")
    if not proj["dias"]:
        linhas.append("Nenhuma entrada ou vencimento conhecido nesse período.")
    for dia, evs, saldo in proj["dias"]:
        for e in evs:
            icone = "🟢" if e.valor > 0 else ("🏠" if "conta fixa" in e.descricao else "💳")
            sinal = "+" if e.valor > 0 else "-"
            linhas.append(f"{icone} {dia.strftime('%d/%m')} · {e.descricao}: {sinal}{brl(abs(e.valor))}")
        linhas.append(f"      ↳ saldo: *{brl(saldo)}*{' ⚠️' if saldo < 0 else ''}")
    linhas.append(f"\n📅 Saldo em {proj['fim'].strftime('%d/%m')}: *{brl(proj['saldo_final'])}*")
    if proj["primeiro_negativo"]:
        linhas.append(f"⚠️ *O saldo fica negativo em {proj['primeiro_negativo'].strftime('%d/%m')}.*")
        linhas.append(f"📉 Menor ponto: *{brl(proj['minimo'])}* em {proj['data_minimo'].strftime('%d/%m')}"
                      f" — para não ficar negativo, precisa entrar *{brl(proj['falta'])}* até lá.")
    else:
        linhas.append(f"✅ O saldo não fica negativo no período. Menor ponto: *{brl(proj['minimo'])}* em {proj['data_minimo'].strftime('%d/%m')}.")
    nota = ("_Considera o saldo atual, o vencimento das faturas (lançado + parcelas previstas), entradas e contas fixas "
            "recorrentes detectadas no histórico")
    nota += (f" e a média de gastos do dia a dia. Compras futuras no cartão e gastos fora do padrão não entram._"
             if proj.get("ritmo") else ". Não inclui gastos novos de débito/Pix nem compras futuras no cartão._")
    linhas.append(nota)
    return "\n".join(linhas)
