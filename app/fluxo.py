"""
Fluxo de caixa projetado: saldo das contas correntes dia a dia, com as saídas conhecidas (vencimento das
faturas, contas fixas recorrentes), as entradas recorrentes (salário, INSS...) e o ritmo médio de gastos
do dia a dia (débito/Pix/boleto).

Funções puras (sem banco): quem lê os dados é app.dados_banco.fluxo_caixa.
Convenção: valor > 0 = entra dinheiro; valor < 0 = sai.
"""
from __future__ import annotations

import math
import re
import statistics
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from app import utils

# Entradas que podem se repetir (vale para a categoria OU a subcategoria). Transferência de terceiros só vale se
# a MESMA pessoa aparecer em 2+ meses com valor parecido (regra geral abaixo): uma transferência avulsa não conta.
SUBCATEGORIAS_RECORRENTES = {"salario", "salário", "salario e pro-labore", "salário e pró-labore", "freelance e bonus", "freelance e bônus",
                             "aposentadoria e beneficios", "aposentadoria e benefícios", "aposentadoria", "beneficios inss",
                             "transferencias de terceiros", "transferências de terceiros",
                             "transferencia recebida", "transferência recebida"}
# Conta fixa = pagamento que se repete igual: boleto, débito automático, mensalidade. Compra de débito e Pix
# enviado NÃO são conta fixa (supermercado, posto, farmácia): vão para a média diária.
_PALAVRAS_FIXA = ("boleto", "debito automatico", "deb aut", "mensalidade", "tarifa", "seguro", "financiamento")
_PREFIXOS_NAO_FIXA = ("compra", "pix", "transferencia", "saque")
_TOLERANCIA_FIXA = 0.05
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


def _recorrentes(linhas: list[dict], hoje: date, dias: int, sinal: int, rotulo: str, minimo: float = 0.0,
                 tolerancia: float = _TOLERANCIA) -> tuple[list[Evento], set[str]]:
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
        parecidos = [x for x in itens if mediana and abs(x[1] - mediana) <= mediana * tolerancia]
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
    fixas = []
    for r in despesas:
        d = utils._normalize(r.get("description") or "")
        if d.startswith(_PREFIXOS_NAO_FIXA) or not any(p in d for p in _PALAVRAS_FIXA):
            continue
        fixas.append(r)
    return _recorrentes(fixas, hoje, dias, -1, "conta fixa", minimo, _TOLERANCIA_FIXA)


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


# ── Ritmo dos gastos do dia a dia: média simples ou ajustada por dia da semana e parte do mês ──────────────────
JANELA_SAZONAL = 180        # dias de histórico usados para achar o padrão (dia da semana / parte do mês)
MIN_DIAS_SAZONAL = 90       # cobertura mínima do histórico para ajustar os fatores
K_SEMANA, K_MES = 4.0, 10.0  # "amostras de dúvida": com poucos dias num grupo, o fator volta para 1
GANHO_MIN = 0.03            # o ajuste só vale se errar pelo menos 3% menos que a média simples no teste com o histórico
_DIAS_SEMANA = ("segundas", "terças", "quartas", "quintas", "sextas", "sábados", "domingos")
_PARTES_MES = ("no início do mês (dias 1 a 10)", "no meio do mês (dias 11 a 20)", "no fim do mês (dia 21 em diante)")


def _parte_mes(d: date) -> int:
    return 0 if d.day <= 10 else (1 if d.day <= 20 else 2)


@dataclass(frozen=True)
class Ritmo:
    """Gasto diário previsto. `sazonal=False`: o mesmo valor todo dia (média). `sazonal=True`: a média vezes o fator do
    dia da semana e da parte do mês (normalizado, para o nível geral continuar igual ao dos últimos dias)."""
    media: float
    sem: tuple = (1.0,) * 7
    mes: tuple = (1.0,) * 3
    norm: float = 1.0
    sazonal: bool = False
    ganho: float = 0.0       # quanto o ajuste errou a menos que a média, no teste com o histórico (0.25 = 25%)

    def __call__(self, d: date) -> float:
        if not self.sazonal:
            return self.media
        return self.media * self.sem[d.weekday()] * self.mes[_parte_mes(d)] / self.norm

    def descricao(self) -> str:
        if not self.sazonal:
            return ""
        altos = [_DIAS_SEMANA[i] for i, f in enumerate(self.sem) if f >= 1.2]
        partes = [_PARTES_MES[i] for i, f in enumerate(self.mes) if f >= 1.15]
        onde = " e ".join(x for x in (", ".join(altos), ", ".join(partes)) if x)
        return "ajustada pelo seu histórico" + (f" (maiores em {onde})" if onde else "")


def _diarios(despesas: list[dict], excluir: set[str], ini: date, fim: date) -> list[tuple[date, float]]:
    """Gasto variável por dia entre `ini` e `fim` (dias sem gasto valem 0), sem contas fixas e sem gasto único enorme."""
    tot: dict[date, float] = {}
    for r in despesas:
        dia = _dia(r)
        if dia is None or not (ini <= dia <= fim) or _chave(r.get("description") or "") in excluir:
            continue
        try:
            v = abs(float(r["amount"]))
        except (TypeError, ValueError, KeyError):
            continue
        if v <= TETO_EXCEPCIONAL:
            tot[dia] = tot.get(dia, 0.0) + v
    return [(ini + timedelta(days=i), tot.get(ini + timedelta(days=i), 0.0)) for i in range((fim - ini).days + 1)]


def _fatores(dias: list[tuple[date, float]]) -> tuple[tuple, tuple] | None:
    """Fator por dia da semana e por parte do mês (gasto médio do grupo / gasto médio geral), puxado para 1 com poucos dias."""
    if not dias:
        return None
    media = sum(v for _d, v in dias) / len(dias)
    if media <= 0:
        return None
    s_sem, n_sem, s_mes, n_mes = [0.0] * 7, [0] * 7, [0.0] * 3, [0] * 3
    for d, v in dias:
        s_sem[d.weekday()] += v; n_sem[d.weekday()] += 1
        s_mes[_parte_mes(d)] += v; n_mes[_parte_mes(d)] += 1
    sem = tuple(((s_sem[i] / n_sem[i] / media) * n_sem[i] + K_SEMANA) / (n_sem[i] + K_SEMANA) if n_sem[i] else 1.0 for i in range(7))
    mes = tuple(((s_mes[i] / n_mes[i] / media) * n_mes[i] + K_MES) / (n_mes[i] + K_MES) if n_mes[i] else 1.0 for i in range(3))
    return sem, mes


def _ritmo_ate(despesas: list[dict], excluir: set[str], corte: date) -> Ritmo:
    """Ritmo ajustado usando só dados até `corte` (inclusive). Sem histórico suficiente: média simples (sazonal=False)."""
    media = ritmo_diario([r for r in despesas if (_dia(r) or corte) <= corte], excluir, corte)
    datas = [d for d in (_dia(r) for r in despesas) if d is not None and d <= corte]
    if not datas:
        return Ritmo(media)
    ini = max(min(datas), corte - timedelta(days=JANELA_SAZONAL - 1))
    if (corte - ini).days + 1 < MIN_DIAS_SAZONAL:
        return Ritmo(media)
    fat = _fatores(_diarios(despesas, excluir, ini, corte - timedelta(days=1)))   # o dia do corte está incompleto
    if fat is None:
        return Ritmo(media)
    sem, mes = fat
    ref = [corte - timedelta(days=i) for i in range(JANELA_RITMO)]
    norm = sum(sem[d.weekday()] * mes[_parte_mes(d)] for d in ref) / len(ref)
    return Ritmo(media, sem, mes, norm or 1.0, True)


def ritmo_projecao(despesas: list[dict], excluir: set[str], hoje: date, passos: int = 4, horizonte: int = 14) -> Ritmo:
    """Escolhe o ritmo do fluxo. O ajuste por dia da semana e parte do mês só é usado se, num teste com o próprio
    histórico (prever os `horizonte` dias seguintes a vários cortes passados), errar menos que a média simples."""
    atual = _ritmo_ate(despesas, excluir, hoje)
    if not atual.sazonal:
        return atual
    err_media, err_saz = [], []
    cortes = 0
    for k in range(1, passos + 1):
        corte = hoje - timedelta(days=horizonte * k)
        r = _ritmo_ate(despesas, excluir, corte)
        if not r.sazonal:
            continue
        cortes += 1
        for d, v in _diarios(despesas, excluir, corte + timedelta(days=1), corte + timedelta(days=horizonte)):
            err_media.append(abs(v - r.media))
            err_saz.append(abs(v - r(d)))
    if cortes < 2 or not err_media:
        return Ritmo(atual.media)           # histórico curto demais para provar que o ajuste ajuda
    em, es = sum(err_media) / len(err_media), sum(err_saz) / len(err_saz)
    if em <= 0 or es >= em * (1 - GANHO_MIN):
        return Ritmo(atual.media)
    return Ritmo(atual.media, atual.sem, atual.mes, atual.norm, True, round(1 - es / em, 2))


def projetar(saldo_inicial: float, eventos: list[Evento], hoje: date, dias: int, ritmo=0.0) -> dict:
    """Linha do tempo só com os dias que têm movimento (o ritmo diário é descontado dia a dia entre eles). `ritmo`:
    número (mesmo gasto todo dia) ou `Ritmo`. Retorna:
    {saldo_inicial, dias: [(data, [Evento], saldo_apos)], minimo, data_minimo, primeiro_negativo, falta,
     saldo_final, fim, ritmo, ritmo_texto, horizonte}"""
    fim = hoje + timedelta(days=dias)
    gasto = ritmo if callable(ritmo) else (lambda _d, v=float(ritmo or 0.0): v)
    media = float(getattr(ritmo, "media", ritmo) or 0.0)
    tem_ritmo = media > 0
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
        if tem_ritmo and gap > 0:
            for k in range(1, gap + 1):
                dia = anterior + timedelta(days=k)
                saldo -= gasto(dia)
                if primeiro_neg is None and saldo < 0:
                    primeiro_neg = dia
            saldo = round(saldo, 2)
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
            "saldo_final": saldo, "fim": fim, "ritmo": round(media, 2),
            "ritmo_texto": ritmo.descricao() if hasattr(ritmo, "descricao") else "", "horizonte": dias}


def brl(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-R$ {s}" if v < 0 else f"R$ {s}"


def formatar(proj: dict | None, sem_contas: bool = False) -> str:
    if sem_contas or proj is None:
        return "📈 Não achei contas correntes conectadas para projetar o fluxo de caixa."
    linhas = [f"📈 *Fluxo de caixa — próximos {proj['horizonte']} dias*",
              f"\n🏦 Saldo hoje (contas correntes): *{brl(proj['saldo_inicial'])}*"]
    if proj.get("ritmo") and proj.get("ritmo_texto"):
        linhas.append(f"🛒 Gastos do dia a dia: média de *{brl(proj['ritmo'])}/dia*, {proj['ritmo_texto']}")
    elif proj.get("ritmo"):
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


# ── Dados para o painel (a mesma projeção, sem texto de WhatsApp) ────────────────────────────────────────
_PREFIXOS_NOME = ("pagamento de boleto ", "pagamento de ", "entrada ", "pix recebido ", "transferencia recebida ", "transferência recebida ")
_MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "em", "para"}


def _nome_limpo(descricao: str) -> tuple[str, str]:
    """('Cond Residencial Tor Rochelle', 'conta') a partir da descrição em texto do evento. Tipos: entrada | conta | fatura | outro."""
    d = (descricao or "").strip()
    tipo = "outro"
    m = re.search(r"\(([^)]*)\)\s*$", d)
    if m:
        marca = m.group(1).lower()
        tipo = "conta" if "conta fixa" in marca else ("fatura" if "fatura" in marca else ("entrada" if "previst" in marca else "outro"))
        d = d[:m.start()].strip()
    for pre in _PREFIXOS_NOME:
        if d.lower().startswith(pre):
            d = d[len(pre):].strip()
            break
    d = " ".join(t for t in d.split() if not (t.isdigit() and len(t) >= 8))      # CPF/códigos longos não ajudam a ler
    if d and d == d.upper():
        d = " ".join(t.lower() if t.lower() in _MINUSCULAS and i else t.capitalize() for i, t in enumerate(d.split()))
    return d or "Lançamento", tipo


def para_painel(proj: dict | None, hoje: date | None = None) -> dict:
    """Projeção do fluxo como dados: situação, de onde vem o saldo final, série para o gráfico e dias com eventos."""
    if proj is None:
        return {"vazio": True}
    dias, serie, entradas, contas = [], [], 0.0, 0.0
    inicio = hoje or proj["fim"] - timedelta(days=proj["horizonte"])
    serie.append({"data": inicio.isoformat(), "saldo": proj["saldo_inicial"]})
    for dia, evs, saldo in proj["dias"]:
        itens = []
        for e in evs:
            nome, tipo = _nome_limpo(e.descricao)
            if e.valor > 0:
                entradas += e.valor
                tipo = "entrada"
            else:
                contas += -e.valor
                tipo = tipo if tipo in ("conta", "fatura") else "conta"
            itens.append({"nome": nome, "tipo": tipo, "valor": round(e.valor, 2)})
        dias.append({"data": dia.isoformat(), "saldo": saldo, "eventos": itens})
        serie.append({"data": dia.isoformat(), "saldo": saldo})
    serie.append({"data": proj["fim"].isoformat(), "saldo": proj["saldo_final"]})
    gasto_dia = round(proj["saldo_inicial"] + entradas - contas - proj["saldo_final"], 2)
    return {
        "vazio": False, "horizonte": proj["horizonte"], "inicio": inicio.isoformat(), "fim": proj["fim"].isoformat(),
        "saldo_inicial": proj["saldo_inicial"], "saldo_final": proj["saldo_final"],
        "minimo": proj["minimo"], "data_minimo": proj["data_minimo"].isoformat(), "falta": proj["falta"],
        "primeiro_negativo": proj["primeiro_negativo"].isoformat() if proj["primeiro_negativo"] else None,
        "ritmo": proj.get("ritmo") or 0.0, "ritmo_texto": proj.get("ritmo_texto") or "",
        "entradas": round(entradas, 2), "contas": round(contas, 2), "gastos_dia": max(gasto_dia, 0.0),
        "serie": serie, "dias": dias,
    }
