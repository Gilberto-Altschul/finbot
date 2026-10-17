"""
Faturas e saldos a partir dos DADOS REAIS da Pluggy (/accounts e /bills).

Por que existe: o FinBot assumia um dia de corte fixo, mas o banco muda a data de
fechamento todo mês (ex.: 22/05, 24/06, 25/07, 25/08, 24/09). O /bills traz a data
exata de cada fatura; aqui ela vira a régua para (1) atribuir cada compra à fatura
certa e (2) mostrar saldo / faturas como o banco mostra.

A parte de cálculo é pura (sem rede) para ser testável; só `carregar_*` e `mensagem_*`
falam com a Pluggy.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app import bancos, itens

logger = logging.getLogger(__name__)

# Quando o banco não informa o fechamento (ocorre em faturas antigas), estima-se
# o fechamento como vencimento - 7 dias (padrão observado nas faturas reais).
DIAS_FECHAMENTO_ATE_VENCIMENTO = 7
# Tolerância (dias) após o vencimento para considerar um pagamento como "dessa" fatura.
JANELA_PAGAMENTO_DIAS = 15

_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


# ── Modelo ───────────────────────────────────────────────────────────────────
@dataclass
class Ciclo:
    fechamento: date
    vencimento: date
    total: float | None = None
    bill_id: str | None = None
    estimado: bool = False            # True = ciclo calculado, não veio do banco
    fechamento_estimado: bool = False  # True = o banco não informou o fechamento
    dia_fixo: int | None = None        # dia fixo de fechamento (ver DIA_FECHAMENTO_FIXO)

    @property
    def mes_ref(self) -> date:
        """Convenção do FinBot: billing_date = dia 1 do mês de vencimento."""
        return date(self.vencimento.year, self.vencimento.month, 1)


# A Pluggy informa fechamentos que não batem com o do banco (ex.: C6 manda 23/05, 25/07, 26/08, 25/09).
# Conferido com as faturas reais: o C6 fecha no dia 23 (na sexta anterior se cair no fim de semana).
# Chave = trecho do nome do cartão/banco (minúsculo).
DIA_FECHAMENTO_FIXO = {"c6": 23}


def dia_fechamento_fixo(nome: str | None) -> int | None:
    n = (nome or "").lower()
    for chave, dia in DIA_FECHAMENTO_FIXO.items():
        if chave in n:
            return dia
    return None


def _fechamento_fixo(venc: date, dia: int) -> date:
    """Fechamento da fatura que vence em `venc`: `dia` do mês anterior, recuando p/ dia útil."""
    ano, mes = (venc.year - 1, 12) if venc.month == 1 else (venc.year, venc.month - 1)
    d = date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def aplicar_fechamento_fixo(ciclos: list["Ciclo"], nome: str | None) -> list["Ciclo"]:
    """Troca o fechamento informado pela Pluggy pelo dia real do banco, quando conhecido."""
    dia = dia_fechamento_fixo(nome)
    if not dia:
        return ciclos
    for c in ciclos:
        c.fechamento = _fechamento_fixo(c.vencimento, dia)
        c.fechamento_estimado = False
        c.dia_fixo = dia
    return ciclos


def _data(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _soma_meses(d: date, n: int) -> date:
    """Soma meses mantendo o dia (limitado ao último dia do mês)."""
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


# ── Montagem dos ciclos ──────────────────────────────────────────────────────
def montar_ciclos(bills: list[dict]) -> list[Ciclo]:
    """Converte o retorno do /bills em ciclos ordenados por vencimento (mais antigo primeiro)."""
    ciclos: list[Ciclo] = []
    for b in bills or []:
        venc = _data(b.get("dueDate"))
        if not venc:
            continue
        fech = _data(b.get("billClosingDate"))
        fech_est = fech is None
        if fech is None:
            fech = venc - timedelta(days=DIAS_FECHAMENTO_ATE_VENCIMENTO)
        total = b.get("totalAmount")
        ciclos.append(Ciclo(fech, venc, float(total) if total is not None else None,
                            b.get("id"), estimado=False, fechamento_estimado=fech_est))
    ciclos.sort(key=lambda c: c.vencimento)
    return ciclos


def _dia_venc_tipico(ciclos: list[Ciclo]) -> int | None:
    """Dia de vencimento mais comum nas últimas faturas reais (o banco antecipa/posterga
    o vencimento em fins de semana; a moda ignora esses desvios)."""
    dias = [c.vencimento.day for c in ciclos if not c.estimado][-6:]
    if not dias:
        return None
    return max(set(dias), key=lambda d: (dias.count(d), dias[::-1].index(d) * -1))


def proximo_ciclo(ultimo: Ciclo, dia_venc: int | None = None) -> Ciclo:
    """Estima o ciclo seguinte: repete o dia de fechamento do último e o dia típico de vencimento."""
    base = _soma_meses(date(ultimo.vencimento.year, ultimo.vencimento.month, 1), 1)
    dia = min(dia_venc or ultimo.vencimento.day, calendar.monthrange(base.year, base.month)[1])
    venc = base.replace(day=dia)
    if ultimo.dia_fixo:
        return Ciclo(_fechamento_fixo(venc, ultimo.dia_fixo), venc, None, None,
                     estimado=True, fechamento_estimado=True, dia_fixo=ultimo.dia_fixo)
    return Ciclo(_soma_meses(ultimo.fechamento, 1), venc,
                 None, None, estimado=True, fechamento_estimado=True)


def ciclo_da_compra(d: date, ciclos: list[Ciclo], hoje: date | None = None) -> tuple[Ciclo, bool] | None:
    """
    Descobre a fatura de uma compra feita em `d`.
    Regra do banco: compra até o dia do fechamento (inclusive) entra nessa fatura.
    Retorna (ciclo, fechada). `fechada` = o fechamento desse ciclo já passou.
    Sem ciclos conhecidos -> None (quem chama usa a regra antiga do dia de corte).
    """
    if not ciclos:
        return None
    hoje = hoje or date.today()
    for c in ciclos:
        if d <= c.fechamento:
            return c, hoje > c.fechamento
    c, dia_venc = ciclos[-1], _dia_venc_tipico(ciclos)
    for _ in range(36):  # teto de segurança (3 anos)
        c = proximo_ciclo(c, dia_venc)
        if d <= c.fechamento:
            return c, hoje > c.fechamento
    return c, hoje > c.fechamento


def ciclo_aberto(ciclos: list[Ciclo], hoje: date | None = None) -> Ciclo | None:
    """Fatura que está acumulando compras agora (primeiro fechamento >= hoje)."""
    if not ciclos:
        return None
    hoje = hoje or date.today()
    for c in ciclos:
        if c.fechamento >= hoje and not c.fechamento_estimado:
            return c
    c, dia_venc = ciclos[-1], _dia_venc_tipico(ciclos)
    for _ in range(36):
        c = proximo_ciclo(c, dia_venc)
        if c.fechamento >= hoje:
            return c
    return c


# ── Pagamento ────────────────────────────────────────────────────────────────
def _pagamentos(bills: list[dict]) -> list[tuple[date, float]]:
    """Todos os pagamentos de todas as faturas, sem duplicar (o banco repete o mesmo
    pagamento dentro de faturas vizinhas)."""
    vistos, saida = set(), []
    for b in bills or []:
        for p in b.get("payments") or []:
            d, v = _data(p.get("paymentDate")), p.get("amount")
            if d is None or v is None:
                continue
            chave = (p.get("id"), d, round(float(v), 2))
            if chave in vistos:
                continue
            vistos.add(chave)
            saida.append((d, float(v)))
    return saida


def status_pagamento(c: Ciclo, pagamentos: list[tuple[date, float]], hoje: date | None = None) -> str:
    """
    'paga' | 'a_vencer' | 'sem_pagamento'.
    Não soma `payments` por fatura: o banco anexa à fatura N o pagamento da N-1.
    Procura, no conjunto de todos os pagamentos, um com o valor do total e data
    entre o fechamento e o vencimento (+ tolerância).
    """
    hoje = hoje or date.today()
    if c.total is None:
        return "a_vencer" if c.vencimento >= hoje else "sem_pagamento"
    if c.total <= 0.005:
        return "paga"
    limite = c.vencimento + timedelta(days=JANELA_PAGAMENTO_DIAS)
    for d, v in pagamentos:
        if abs(v - c.total) <= 0.01 and c.fechamento <= d <= limite:
            return "paga"
    return "a_vencer" if c.vencimento >= hoje else "sem_pagamento"


# ── Formatação ───────────────────────────────────────────────────────────────
def fmt_moeda(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dm(d: date) -> str:
    return d.strftime("%d/%m")


def rotulo_mes(c: Ciclo) -> str:
    return f"{_MESES[c.vencimento.month - 1].capitalize()}/{str(c.vencimento.year)[2:]}"


_ICONE = {"paga": "✅ paga", "a_vencer": "⏳ a vencer", "sem_pagamento": "⚠️ sem pagamento identificado"}


def status_dos_ciclos(bills: list[dict], hoje: date | None = None) -> list[tuple[Ciclo, str]]:
    """Ciclos do /bills já com o status de pagamento ('paga' | 'a_vencer' | 'sem_pagamento')."""
    pag = _pagamentos(bills)
    return [(c, status_pagamento(c, pag, hoje)) for c in montar_ciclos(bills)]


def formatar_lista_faturas(itens: list[tuple[Ciclo, str]], saldo_aberto: float | None, nome_cartao: str = "",
                           hoje: date | None = None, limite: int = 6) -> str:
    """itens = [(Ciclo, status)] ordenados do mais antigo ao mais novo."""
    hoje = hoje or date.today()
    if not itens:
        return "💳 Não encontrei faturas deste cartão ainda."
    recentes = itens[-limite:]
    titulo = f"💳 *Faturas do cartão{(' ' + nome_cartao) if nome_cartao else ''}*"
    linhas = [titulo, ""]
    for c, st in reversed(recentes):
        total = f"R$ {fmt_moeda(c.total)}" if c.total is not None else "valor indisponível"
        fech = f"fechou {_dm(c.fechamento)}" + ("*" if c.fechamento_estimado else "")
        linhas.append(f"{rotulo_mes(c)}  {fech}  venc. {_dm(c.vencimento)}\n   {total}  {_ICONE[st]}")
    if any(c.fechamento_estimado for c, _ in recentes):
        linhas.append("\n_* fechamento estimado (o banco não informou)_")
    aberto = ciclo_aberto([c for c, _ in itens], hoje)
    if aberto is not None and saldo_aberto is not None:
        linhas.append(f"\n🟡 *Aberta agora:* R$ {fmt_moeda(saldo_aberto)} "
                      f"(fecha ~{_dm(aberto.fechamento)}, vence ~{_dm(aberto.vencimento)})")
    return "\n".join(linhas)


def formatar_faturas(bills: list[dict], saldo_aberto: float | None, nome_cartao: str = "",
                     hoje: date | None = None, limite: int = 6) -> str:
    return formatar_lista_faturas(status_dos_ciclos(bills, hoje), saldo_aberto, nome_cartao, hoje, limite)


def formatar_aberta(ciclos: list[Ciclo], saldo_aberto: float | None, hoje: date | None = None) -> str:
    hoje = hoje or date.today()
    if saldo_aberto is None:
        return "💳 Não consegui ler a fatura aberta do cartão agora."
    aberto = ciclo_aberto(ciclos, hoje)
    msg = f"💳 *Fatura aberta:* R$ {fmt_moeda(saldo_aberto)}"
    if aberto is not None:
        msg += (f"\nFecha{' por volta de' if aberto.estimado else ''} {_dm(aberto.fechamento)} · "
                f"vence{' por volta de' if aberto.estimado else ''} {_dm(aberto.vencimento)}")
        if aberto.estimado:
            msg += "\n_(datas estimadas pelo padrão das últimas faturas)_"
    return msg


def formatar_fatura_aberta(bills: list[dict], saldo_aberto: float | None, hoje: date | None = None) -> str:
    return formatar_aberta(montar_ciclos(bills), saldo_aberto, hoje)


def _atualizado_em(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            local = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
        except Exception:
            local = dt.astimezone(timezone(timedelta(hours=-3)))
        hoje = datetime.now(local.tzinfo).date()
        quando = "hoje" if local.date() == hoje else local.strftime("%d/%m")
        return f"Atualizado {quando}, {local.strftime('%H:%M')}"
    except ValueError:
        return ""


def formatar_saldos(contas: list[dict]) -> str:
    """contas: [{'tipo','nome','saldo','atualizado_iso'}] — tipo = 'BANK' | 'CREDIT'."""
    contas_banco = [c for c in contas if c["tipo"] == "BANK" and c.get("saldo") is not None]
    cartoes = [c for c in contas if c["tipo"] == "CREDIT" and c.get("saldo") is not None]
    if not contas_banco and not cartoes:
        return ""
    linhas = ["💰 *Seus saldos*", ""]
    for c in contas_banco:
        linhas.append(f"{c['nome']}: R$ {fmt_moeda(c['saldo'])}")
    if len(contas_banco) > 1:
        linhas.append(f"*Total em contas: R$ {fmt_moeda(sum(c['saldo'] for c in contas_banco))}*")
    if contas_banco and cartoes:
        linhas.append("")
    for c in cartoes:
        linhas.append(f"{c['nome']} (fatura aberta): R$ {fmt_moeda(c['saldo'])}")
    if len(cartoes) > 1:
        linhas.append(f"*Total em faturas: R$ {fmt_moeda(sum(c['saldo'] for c in cartoes))}*")
    quando = next((_atualizado_em(c.get("atualizado_iso")) for c in contas if c.get("atualizado_iso")), "")
    if quando:
        linhas.append(f"\n_{quando}_")
    return "\n".join(linhas)


# ── Acesso à Pluggy ──────────────────────────────────────────────────────────
def _so_digitos(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _pode_descobrir_itens(user_phone: str) -> bool:
    """Só os números listados em PLUGGY_FALLBACK_PHONES (ex.: o dono, antes de vincular o banco) podem
    usar a descoberta automática de itens / item padrão do .env. Vazio = ninguém."""
    import os
    permitidos = {_so_digitos(p) for p in os.getenv("PLUGGY_FALLBACK_PHONES", "").split(",") if _so_digitos(p)}
    return _so_digitos(user_phone) in permitidos


async def _ids_dos_itens(pluggy, user_phone: str) -> list[str]:
    """Une os itens listados pela API, o item padrão do .env e o da conta escolhida pelo usuário."""
    import app.database as db
    from app.config import get_settings

    ids: list[str] = list(itens.ler_ids(user_phone))   # bancos já vinculados ao usuário
    if ids:
        # A tabela do usuário é a FONTE DA VERDADE: o item do .env e a listagem da Pluggy só servem para
        # descobrir o primeiro banco. Senão um item de teste do .env voltaria mesmo depois de removido.
        return list(dict.fromkeys(ids))
    if not _pode_descobrir_itens(user_phone):
        # Usuário novo: NÃO descobre bancos sozinho. A listagem da Pluggy traz os itens de TODA a aplicação
        # (inclusive os do dono); cada pessoa precisa conectar o seu com "adicionar banco <código>".
        return []
    try:
        for it in await pluggy.listar_itens():
            if it.get("id"):
                ids.append(it["id"])
    except Exception as e:
        logger.warning(f"Listagem de itens falhou ({e}); usando itens conhecidos.")
    try:
        padrao = db.get_pluggy_conta_padrao(user_phone)
        if padrao and padrao.get("item_id"):
            ids.append(padrao["item_id"])
    except Exception as e:
        logger.warning(f"Conta padrão indisponível: {e}")
    default = getattr(get_settings(), "default_item_id", None)
    if default:
        ids.append(default)
    return list(dict.fromkeys(ids))


async def carregar_contas(pluggy, user_phone: str) -> list[dict]:
    """Lista normalizada das contas de todos os itens conhecidos."""
    contas: list[dict] = []
    for item_id in await _ids_dos_itens(pluggy, user_phone):
        try:
            item = await pluggy.obter_item(item_id)
            for c in await pluggy.listar_contas(item_id):
                tipo = c.get("type")
                if tipo not in ("BANK", "CREDIT"):
                    continue
                nome = c.get("marketingName") or c.get("name") or (
                    "Cartão de crédito" if tipo == "CREDIT" else "Conta corrente")
                cd = c.get("creditData") or {}
                contas.append({"id": c.get("id"), "item_id": item_id, "tipo": tipo, "nome": nome,
                               "saldo": c.get("balance"), "atualizado_iso": item.get("lastUpdatedAt"),
                               "limite": cd.get("creditLimit"), "limite_disponivel": cd.get("availableCreditLimit"),
                               "numero": c.get("number"),
                               "banco_codigo": bancos.codigo_do_banco((c.get("bankData") or {}).get("transferNumber"))})
        except Exception as e:
            logger.warning(f"Falha ao ler contas do item {item_id}: {e}")
    return bancos.aplicar_nomes(contas)
