"""
Dados do banco (saldos e faturas) guardados no Supabase.

Os comandos leem daqui; a Pluggy só é consultada na 1ª vez do dia (ou no "sincronizar").
Motivo: o banco renova os dados poucas vezes por dia — perguntar a cada comando não traz nada
novo, só custa tempo e deixa o bot dependente da Pluggy estar no ar. Se ela cair, o último dado
guardado é mostrado com um aviso.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta

import app.database as db
from app import faturas as fat
from app import parcelas as parcelas_proj
from app import fluxo as fluxo_mod
from app import previsao as previsao_mod
from app import demo as demo_mod
from calendar import monthrange

from app import bancos, itens as itens_mod, revisao, titular
from app.utils import get_lookup_prefix, _normalize

logger = logging.getLogger(__name__)

# Muda a cada pacote entregue; o comando 'versao' mostra isto para conferir a publicação.
VERSAO = "2026-10-28k · painel: botão para voltar ao WhatsApp"

_MESES_COMPLETOS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                    "setembro", "outubro", "novembro", "dezembro"]

T_CONTAS, T_HIST, T_FATURAS = "finbot_contas", "finbot_saldo_historico", "finbot_faturas"


# ── tempo ────────────────────────────────────────────────────────────────────
def _fuso():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Sao_Paulo")
    except Exception:
        return timezone(timedelta(hours=-3))


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dia_local(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(_fuso()).date()
    except ValueError:
        return None


def hoje_local() -> date:
    return datetime.now(_fuso()).date()


# ── leitura ──────────────────────────────────────────────────────────────────
def ordenar(contas: list[dict]) -> list[dict]:
    """Ordem estável (contas correntes antes dos cartões): é ela que o usuário vê em 'contas'
    e usa em 'apelido <n> <nome>'."""
    # Usa o nome AUTOMÁTICO (não o apelido): renomear uma conta não pode embaralhar os números da lista.
    return sorted(contas, key=lambda c: (c["tipo"] != "BANK", _normalize(c.get("nome_auto") or c.get("nome") or ""), c["id"]))


def ler_contas(user_phone: str) -> list[dict]:
    res = db.get_db().table(T_CONTAS).select("*").eq("user_key", get_lookup_prefix(user_phone)).execute()
    return ordenar([{"id": r["account_id"], "item_id": r.get("item_id"), "tipo": r["tipo"],
                     "nome": r.get("apelido") or r.get("nome"), "nome_auto": r.get("nome"), "apelido": r.get("apelido"),
                     "saldo": None if r.get("saldo") is None else float(r["saldo"]),
                     "atualizado_iso": r.get("atualizado_banco"), "sincronizado_em": r.get("sincronizado_em")}
                    for r in (res.data or [])])


def ler_faturas(user_phone: str, account_id: str) -> list[tuple[fat.Ciclo, str]]:
    """[(Ciclo, status)] do mais antigo ao mais novo, a partir do que está guardado."""
    res = (db.get_db().table(T_FATURAS).select("*")
           .eq("user_key", get_lookup_prefix(user_phone)).eq("account_id", account_id).execute())
    hoje = hoje_local()
    itens = []
    for r in sorted(res.data or [], key=lambda r: r["vencimento"]):
        c = fat.Ciclo(date.fromisoformat(str(r["fechamento"])[:10]), date.fromisoformat(str(r["vencimento"])[:10]),
                      None if r.get("total") is None else float(r["total"]), r["bill_id"],
                      fechamento_estimado=bool(r.get("fechamento_estimado")))
        st = "paga" if r.get("paga") else ("a_vencer" if c.vencimento >= hoje else "sem_pagamento")
        itens.append((c, st))
    return itens


def nomes_cartoes(user_phone: str) -> list[str]:
    """Nomes (apelido ou banco + cartão) dos cartões de crédito guardados."""
    try:
        return [c["nome"] for c in ler_contas(user_phone) if c["tipo"] == "CREDIT" and c.get("nome")]
    except Exception:
        return []


def _cartoes_com_ciclos(user_phone: str, account_id: str | None = None):
    for c in ler_contas(user_phone):
        if c["tipo"] != "CREDIT" or (account_id and c["id"] != account_id):
            continue
        ciclos = [ci for ci, _ in ler_faturas(user_phone, c["id"])]
        if ciclos:
            yield c, ciclos


def vencimento_do_mes(user_phone: str, ano: int, mes: int, account_id: str | None = None) -> date | None:
    """Vencimento REAL (ou estimado pelo padrão do banco) da fatura que vence em ano/mês.
    Com `account_id`, só daquele cartão. Usa só o que está guardado; None se não houver nada."""
    try:
        for _c, ciclos in _cartoes_com_ciclos(user_phone, account_id):
            for ci in ciclos:
                if (ci.vencimento.year, ci.vencimento.month) == (ano, mes):
                    return ci.vencimento
            dia = fat._dia_venc_tipico(ciclos)
            ultimo = ciclos[-1]
            for _ in range(36):
                if (ultimo.vencimento.year, ultimo.vencimento.month) >= (ano, mes):
                    break
                ultimo = fat.proximo_ciclo(ultimo, dia)
                if (ultimo.vencimento.year, ultimo.vencimento.month) == (ano, mes):
                    return ultimo.vencimento
            if dia:  # mês anterior ao histórico guardado: mantém o dia típico
                return fat.proximo_dia_util(date(ano, mes, min(dia, monthrange(ano, mes)[1])))
    except Exception as e:
        logger.warning(f"vencimento_do_mes falhou: {e}")
    return None


def proximo_vencimento(user_phone: str, hoje: date | None = None) -> date | None:
    """Vencimento da próxima fatura a vencer (a aberta, ou a fechada ainda não vencida)."""
    hoje = hoje or hoje_local()
    try:
        for _c, ciclos in _cartoes_com_ciclos(user_phone):
            for ci in ciclos:
                if ci.vencimento >= hoje:
                    return ci.vencimento
            dia, ultimo = fat._dia_venc_tipico(ciclos), ciclos[-1]
            for _ in range(36):
                ultimo = fat.proximo_ciclo(ultimo, dia)
                if ultimo.vencimento >= hoje:
                    return ultimo.vencimento
    except Exception as e:
        logger.warning(f"proximo_vencimento falhou: {e}")
    return None


def esta_em_dia(contas: list[dict], hoje: date | None = None) -> bool:
    """Em dia = existe dado e TODAS as contas foram buscadas hoje (horário de Brasília)."""
    hoje = hoje or hoje_local()
    return bool(contas) and all(_dia_local(c.get("sincronizado_em")) == hoje for c in contas)


# ── escrita ──────────────────────────────────────────────────────────────────
async def atualizar(pluggy_factory, user_phone: str) -> int:
    """Busca na Pluggy e grava contas, histórico de saldo e faturas. Devolve nº de contas gravadas."""
    if demo_mod.eh_telefone_demo(user_phone):   # demonstração: nunca fala com a Pluggy (nem vincula o item real)
        raise RuntimeError("modo demonstração: sem banco conectado")
    pluggy = pluggy_factory()
    contas = await fat.carregar_contas(pluggy, user_phone)
    if not contas:
        raise RuntimeError("A Pluggy não devolveu nenhuma conta.")

    key, agora = get_lookup_prefix(user_phone), _agora_iso()
    anteriores = {r["account_id"]: r for r in (db.get_db().table(T_CONTAS).select("*").eq("user_key", key).execute().data or [])}

    linhas, historico = [], []
    for c in contas:
        linhas.append({"user_key": key, "account_id": c["id"], "item_id": c["item_id"], "tipo": c["tipo"],
                       "nome": c["nome"], "banco_nome": c.get("banco"), "saldo": c["saldo"], "credit_limit": c.get("limite"),
                       "available_limit": c.get("limite_disponivel"),
                       "atualizado_banco": c.get("atualizado_iso"), "sincronizado_em": agora})
        ant = anteriores.get(c["id"])
        if c["saldo"] is not None and (ant is None or ant.get("saldo") is None
                                       or round(float(ant["saldo"]), 2) != round(float(c["saldo"]), 2)):
            historico.append({"user_key": key, "account_id": c["id"], "saldo": c["saldo"], "capturado_em": agora})
    # `apelido` NÃO está em `linhas` de propósito: o upsert preserva o que o usuário definiu.
    db.get_db().table(T_CONTAS).upsert(linhas, on_conflict="user_key,account_id").execute()
    titular.registrar(user_phone, contas)     # nome do titular (pessoa física) → reconhece Pix entre contas próprias
    vistos: dict[str, tuple] = {}
    for c in contas:
        if c.get("item_id") and (c["item_id"] not in vistos or c.get("banco_codigo")):
            vistos[c["item_id"]] = (c.get("banco_codigo"), c.get("banco"))
    for item_id, (cod, nome_banco) in vistos.items():
        itens_mod.registrar(user_phone, item_id, cod, nome_banco)   # auto-vincula: o item do .env passa a ser do usuário
    if historico:
        db.get_db().table(T_HIST).insert(historico).execute()

    for c in (c for c in contas if c["tipo"] == "CREDIT"):
        try:
            bills = await pluggy.listar_faturas(c["id"])
        except Exception as e:  # um cartão sem /bills não pode derrubar o resto
            logger.warning(f"Faturas indisponíveis para {c['id']}: {e}")
            continue
        itens = fat.status_dos_ciclos(bills, hoje_local())
        if itens:
            db.get_db().table(T_FATURAS).upsert(
                [{"user_key": key, "bill_id": ci.bill_id, "account_id": c["id"], "vencimento": ci.vencimento.isoformat(),
                  "fechamento": ci.fechamento.isoformat(), "fechamento_estimado": ci.fechamento_estimado,
                  "total": ci.total, "paga": st == "paga", "sincronizado_em": agora} for ci, st in itens],
                on_conflict="user_key,bill_id").execute()
    return len(linhas)


async def garantir(pluggy_factory, user_phone: str, forcar: bool = False) -> tuple[list[dict], bool]:
    """Devolve (contas, desatualizado). Busca na Pluggy só se o dado não for de hoje (ou forcar)."""
    guardadas = ler_contas(user_phone)
    if demo_mod.eh_telefone_demo(user_phone):
        return guardadas, False
    if not forcar and esta_em_dia(guardadas):
        return guardadas, False
    try:
        await atualizar(pluggy_factory, user_phone)
        return ler_contas(user_phone), False
    except Exception as e:
        logger.warning(f"Atualização pela Pluggy falhou ({e}).")
        if guardadas:
            return guardadas, True
        raise


# ── mensagens ────────────────────────────────────────────────────────────────
def _aviso(contas: list[dict], desatualizado: bool) -> str:
    if not desatualizado:
        return ""
    dia = next((_dia_local(c.get("sincronizado_em")) for c in contas if c.get("sincronizado_em")), None)
    return f"\n\n⚠️ _Não consegui falar com o banco agora; mostrando os dados de {dia.strftime('%d/%m') if dia else 'antes'}._"


async def mensagem_saldos(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    msg = fat.formatar_saldos(contas)
    return (msg + _aviso(contas, velho)) if msg else ""


async def mensagem_faturas(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    cartoes = [c for c in contas if c["tipo"] == "CREDIT"]
    if not cartoes:
        return "💳 Não encontrei nenhum cartão de crédito conectado."
    partes = [fat.formatar_lista_faturas(ler_faturas(user_phone, c["id"]), c["saldo"], c["nome"], hoje_local()) for c in cartoes]
    return "\n\n".join(partes) + _aviso(contas, velho)


async def mensagem_fatura_aberta(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    cartoes = [c for c in contas if c["tipo"] == "CREDIT"]
    if not cartoes:
        return "💳 Não encontrei nenhum cartão de crédito conectado."
    partes = [fat.formatar_aberta([ci for ci, _ in ler_faturas(user_phone, c["id"])], c["saldo"], hoje_local()) for c in cartoes]
    return "\n\n".join(partes) + _aviso(contas, velho)


async def total_do_banco(pluggy_factory, user_phone: str, vencimento: date, account_id: str | None = None) -> float | None:
    """Total oficial da fatura que vence no mês de `vencimento` (a partir do que está guardado).
    Com `account_id`, só aquele cartão; sem ele, o primeiro que tiver fatura no mês."""
    contas, _ = await garantir(pluggy_factory, user_phone)
    for c in (c for c in contas if c["tipo"] == "CREDIT" and (account_id is None or c["id"] == account_id)):
        for ci, _st in ler_faturas(user_phone, c["id"]):
            if (ci.vencimento.year, ci.vencimento.month) == (vencimento.year, vencimento.month):
                return ci.total
    return None


# ── contas e apelidos ────────────────────────────────────────────────────────
async def mensagem_contas(pluggy_factory, user_phone: str) -> str:
    contas, velho = await garantir(pluggy_factory, user_phone)
    if not contas:
        return "🏦 Ainda não encontrei contas conectadas."
    linhas = ["🏦 *Suas contas*", ""] + [f"{i} · {c['nome']}" for i, c in enumerate(contas, 1)]
    linhas.append("\n_Para mudar o nome: apelido <número> <nome>_")
    return "\n".join(linhas) + _aviso(contas, velho)


def definir_apelido(user_phone: str, indice: int, apelido: str) -> str:
    contas = ler_contas(user_phone)
    apelido = " ".join((apelido or "").split())[:40]
    if not contas or indice < 1 or indice > len(contas):
        return "⚠️ Não achei essa conta. Digite *contas* para ver a lista numerada."
    if not apelido:
        return "⚠️ Me diga o nome. Ex.: apelido 2 Cartão da firma"
    alvo = contas[indice - 1]
    db.get_db().table(T_CONTAS).update({"apelido": apelido}).eq("user_key", get_lookup_prefix(user_phone)).eq("account_id", alvo["id"]).execute()
    return f"✅ Pronto: *{apelido}*"


def achar_contas(contas: list[dict], texto: str, tipo: str | None = None) -> tuple[list[dict], str]:
    base = [c for c in contas if tipo is None or c["tipo"] == tipo]
    return bancos.resolver_conta(base, texto, _normalize)


# ── sincronizar tudo ─────────────────────────────────────────────────────────
async def sincronizar_todas(pluggy, user_phone: str, contas: list[dict]) -> str:
    """Importa as transações de TODAS as contas e faz UMA pergunta de revisão no fim."""
    linhas, n_revisar, n_auto = [], 0, 0
    for c in contas:
        try:
            msg, rows = await pluggy.sync_user_transactions(user_phone, c["id"], c["item_id"], iniciar_revisao=False)
        except Exception as e:
            logger.error(f"Falha ao sincronizar {c['id']}: {e}")
            linhas.append(f"• {c['nome']}: ❌ não consegui sincronizar")
            continue
        if rows:
            n = len(rows)
            r = sum(1 for x in rows if x.get("review_status") == "revisar")
            n_revisar, n_auto = n_revisar + r, n_auto + (n - r)
            linhas.append(f"• {c['nome']}: {n} novo{'s' if n != 1 else ''}")
        elif msg.startswith("❌"):
            linhas.append(f"• {c['nome']}: ❌ erro ao salvar")
        elif "andamento" in msg:
            linhas.append(f"• {c['nome']}: o banco ainda está atualizando, tente de novo daqui a pouco")
        else:
            linhas.append(f"• {c['nome']}: sem novidades")
    texto = "✅ *Sincronizado*\n" + "\n".join(linhas)
    if n_revisar:
        texto += f"\n\n✨ {n_auto} classificados sozinhos · 🤔 {n_revisar} para você confirmar (sem pressa)."
        try:
            pergunta = revisao.iniciar(user_phone)
            if pergunta:
                texto += "\n\n" + pergunta
        except Exception as e:
            logger.error(f"Falha ao iniciar revisão após sincronizar tudo: {e}")
    elif n_auto:
        texto += "\n\n✨ Tudo classificado automaticamente."
    return texto


# ── fatura por cartão ────────────────────────────────────────────────────────
def _gastos_da_fatura(user_phone: str, due: date) -> list[dict]:
    ini = date(due.year, due.month, 1).isoformat()
    fim = date(due.year, due.month, monthrange(due.year, due.month)[1]).isoformat()
    res = (db.get_db().table("finbot_expenses")
           .select("id, category, subcategory, description, amount, purchase_date, billing_date, installment_of, installment_total, account_id")
           .ilike("user_phone", db._q(user_phone)).eq("payment_method", "credito").eq("transaction_type", "expense")
           .gte("billing_date", ini).lte("billing_date", fim).order("purchase_date", desc=True).execute())
    return res.data or []


def _creditos_da_fatura(user_phone: str, due: date) -> list[dict]:
    """Créditos no cartão (estornos, 'Inclusão de Pagamento'...) que o banco abate da fatura."""
    ini = date(due.year, due.month, 1).isoformat()
    fim = date(due.year, due.month, monthrange(due.year, due.month)[1]).isoformat()
    try:
        res = (db.get_db().table("finbot_expenses")
               .select("description, amount, purchase_date, account_id")
               .ilike("user_phone", db._q(user_phone)).eq("payment_method", "credito").eq("transaction_type", "income")
               .gte("billing_date", ini).lte("billing_date", fim).execute())
        return res.data or []
    except Exception as e:
        logger.warning(f"Créditos da fatura indisponíveis: {e}")
        return []


def _bloco_cartao(nome: str, due: date, gastos: list[dict], total_banco: float | None,
                  detalhar: bool, pagina: int, page_size: int = 15, creditos: list[dict] | None = None,
                  fecha_em: date | None = None, previstas: tuple | None = None) -> str:
    """`fecha_em` preenchido = fatura ainda ABERTA (fecha nessa data): mostra "compras até agora" e avisa que as
    parcelas de compras anteriores só entram quando ela fecha, em vez de acusar diferença com o banco."""
    bruto = round(sum(float(g["amount"]) for g in gastos), 2)
    abatido = round(sum(abs(float(c["amount"])) for c in (creditos or [])), 2)
    total = round(bruto - abatido, 2)
    rotulo = "Compras até agora" if fecha_em else "Total no FinBot"
    msg = f"💳 *{nome}* · vence {due.strftime('%d/%m/%Y')}\n💰 {rotulo}: *R$ {fat.fmt_moeda(total)}*\n"
    if abatido:
        msg += f"↩️ Já abatidos créditos/estornos: R$ {fat.fmt_moeda(abatido)}\n"
    if previstas and previstas[0] > 0:
        msg += (f"📅 Parcelas previstas ainda não lançadas: R$ {fat.fmt_moeda(previstas[0])} ({previstas[1]})\n"
                f"💰 Total com a previsão: *R$ {fat.fmt_moeda(round(total + previstas[0], 2))}*\n")
    if fecha_em:
        msg += f"⏳ Fatura aberta: as parcelas de compras anteriores só entram quando ela fecha ({fecha_em.strftime('%d/%m')}).\n"
    elif total_banco is not None:
        msg += f"🏦 Fatura no banco: R$ {fat.fmt_moeda(total_banco)}\n"
        dif = round(total_banco - total, 2)
        if abs(dif) > 0.01:
            msg += f"⚠️ Diferença de R$ {fat.fmt_moeda(abs(dif))} (compras ainda não sincronizadas ou ajustes do banco)\n"
    prev_itens = previstas[2] if previstas and len(previstas) > 2 else []
    if detalhar and pagina == 1 and prev_itens:
        msg += "\n📅 *Parcelas previstas (ainda não lançadas):*\n"
        for desc, valor, n, tot in prev_itens:
            msg += f" • {desc[:28].strip()} ({n}/{tot}): R$ {fat.fmt_moeda(valor)}\n"
    if not gastos:
        return msg + ("\n" if prev_itens and detalhar else "") + "Nenhum lançamento nesta fatura até agora."
    por_cat: dict[str, float] = {}
    for g in gastos:
        por_cat[g.get("category") or "Outros"] = por_cat.get(g.get("category") or "Outros", 0.0) + float(g["amount"])
    top = sorted(por_cat.items(), key=lambda kv: kv[1], reverse=True)[:6]
    if not detalhar or pagina == 1:
        msg += "\n📊 *Por categoria:*\n" + "\n".join(f" • {c}: R$ {fat.fmt_moeda(v)}" for c, v in top) + "\n"
    if detalhar:
        paginas = (len(gastos) + page_size - 1) // page_size
        msg += (f"\n🧾 *Lançamentos* (pág. {pagina}/{paginas}):\n" if paginas > 1 else "\n🧾 *Lançamentos:*\n")
        for n, g in enumerate(gastos[(pagina - 1) * page_size: pagina * page_size], 1):
            dt = str(g.get("purchase_date") or g.get("billing_date") or "")
            data = f"{dt[8:10]}/{dt[5:7]} | " if len(dt) >= 10 else ""
            parc = f" ({g['installment_of']}/{g['installment_total']})" if g.get("installment_of") else ""
            cat = f" · 🏷️ {g['category']}" if g.get("category") else ""
            msg += f" {n}. {data}{g['description']}{parc}: R$ {fat.fmt_moeda(float(g['amount']))}{cat}\n"
        msg += "\n💡 Para corrigir: *acertar [nº] categoria [nome]* (ex.: acertar 3 categoria Lazer) ou *acertar [nº] excluir*."
        if pagina < paginas:
            mes_txt = _MESES_COMPLETOS[due.month - 1]   # sem o mês, a página 2 abriria a fatura atual
            msg += f"\n➡️ Digite *fatura {nome.lower()} {mes_txt} pág {pagina + 1}* para ver mais."
    return msg.rstrip()


async def mensagem_fatura_cartoes(pluggy_factory, user_phone: str, due: date,
                                  texto_cartao: str | None = None, pagina: int = 1) -> str | None:
    """
    Fatura de um cartão específico ('fatura nubank setembro') ou, com 2+ cartões, um bloco por cartão.
    Devolve None quando não se aplica (0 ou 1 cartão sem pedido explícito): o fluxo antigo segue valendo.
    """
    cartoes = [c for c in ler_contas(user_phone) if c["tipo"] == "CREDIT"]
    if not cartoes:
        return None
    aviso = ""
    if texto_cartao and texto_cartao.strip():
        achadas, motivo = achar_contas(cartoes, texto_cartao)
        nomes = ", ".join(c["nome"] for c in cartoes)
        if motivo == "varias":
            return f"⚠️ Qual deles? {', '.join(c['nome'] for c in achadas)}. Ex.: fatura {achadas[0]['nome'].lower()}"
        if motivo == "nenhuma":
            # Texto que não é nome de cartão (a frase pode ter outra palavra solta): não trava o usuário.
            if len(cartoes) < 2:
                return None
            alvo, detalhar, aviso = cartoes, False, f"ℹ️ Não achei o cartão “{texto_cartao.strip()}”; seguem todos ({nomes}).\n\n"
        else:
            alvo, detalhar = achadas, True
    elif len(cartoes) > 1:
        alvo, detalhar = cartoes, False
    else:
        return None
    gastos = _gastos_da_fatura(user_phone, due)
    creditos = _creditos_da_fatura(user_phone, due)
    blocos = []
    for c in alvo:
        # Lançamentos antigos não têm conta: com um cartão só, só podem ser dele.
        proprios = [g for g in gastos if g.get("account_id") == c["id"] or (len(cartoes) == 1 and not g.get("account_id"))]
        try:
            total_banco = await total_do_banco(pluggy_factory, user_phone, due, c["id"])
        except Exception as e:
            logger.warning(f"Total do banco indisponível para {c['nome']}: {e}")
            total_banco = None
        due_c = vencimento_do_mes(user_phone, due.year, due.month, c["id"]) or due   # cada cartão tem o seu vencimento
        creds = [x for x in creditos if x.get("account_id") == c["id"] or (len(cartoes) == 1 and not x.get("account_id"))]
        fecha_em = None
        try:
            ab = fat.ciclo_aberto([ci for ci, _ in ler_faturas(user_phone, c["id"])], hoje_local())
            if ab is not None and ab.fechamento >= hoje_local() and (ab.vencimento.year, ab.vencimento.month) == (due_c.year, due_c.month):
                fecha_em = ab.fechamento
        except Exception as e:
            logger.warning(f"Não consegui saber se a fatura de {c['nome']} está aberta: {e}")
        previstas = None
        if total_banco is None or fecha_em:   # fatura ainda aberta/futura: as parcelas que o banco ainda não lançou entram como previsão
            try:
                meus = [x for x in db.get_active_installments(user_phone) if x.get("account_id") == c["id"]]
                mes_c = date(due_c.year, due_c.month, 1)
                tot_p, n_p = parcelas_proj.previstas_do_mes(meus, mes_c, hoje_local())
                previstas = (tot_p, n_p, parcelas_proj.previstas_detalhe(meus, mes_c, hoje_local()))
            except Exception as e:
                logger.warning(f"Parcelas previstas indisponíveis para {c['nome']}: {e}")
        blocos.append(_bloco_cartao(c["nome"], due_c, proprios, total_banco, detalhar, pagina, creditos=creds, fecha_em=fecha_em,
                                    previstas=previstas))
        if detalhar:
            try:
                from app import tools as _tools   # import tardio: tools importa este módulo
                _tools._SESSAO_LISTAGEM[user_phone] = [g["id"] for g in proprios[(pagina - 1) * 15: pagina * 15] if g.get("id") is not None]
            except Exception as e:
                logger.warning(f"Não registrei a lista da fatura para 'acertar': {e}")
    sem_conta = [g for g in gastos if not g.get("account_id")]
    if sem_conta and len(cartoes) > 1:
        blocos.append(f"📎 {len(sem_conta)} lançamento(s) antigo(s) sem cartão definido "
                      f"(R$ {fat.fmt_moeda(sum(float(g['amount']) for g in sem_conta))}).")
    return aviso + "\n\n".join(blocos)


# ── adicionar banco manualmente (até a conexão automática existir) ───────────
async def adicionar_banco(pluggy_factory, user_phone: str, item_id: str) -> str:
    item_id = (item_id or "").strip()
    if not item_id:
        return "⚠️ Me mande o código do item. Ex.: adicionar banco 8f3a1c2e-..."
    pluggy = pluggy_factory()
    try:
        await pluggy.obter_item(item_id)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(f"adicionar banco: item {item_id!r} recusado pela Pluggy (HTTP {status}): {e}")
        if status in (401, 403) or (status and status >= 500):
            return "⚠️ A Pluggy não respondeu direito agora. Tente de novo em alguns minutos."
        return ("⚠️ Não encontrei esse item na Pluggy. Confira o código e tente de novo.\n"
                "_Dica: o item precisa ter sido conectado pela aplicação do FinBot; itens criados em outra conta da Pluggy (ex.: o MeuPluggy de outra pessoa) não aparecem aqui._")
    itens_mod.registrar(user_phone, item_id)
    contas, _ = await garantir(lambda: pluggy, user_phone, forcar=True)
    novas = [c for c in contas if c.get("item_id") == item_id]
    if not novas:
        return "⚠️ O item existe, mas não trouxe nenhuma conta ainda. Tente de novo em alguns minutos."
    return "✅ Banco conectado:\n" + "\n".join(f"• {c['nome']}" for c in novas) + \
           "\n\nDigite *saldo* para ver, ou *sincronizar* para importar as compras."


def remover_banco(user_phone: str, indice: int) -> str:
    """Desvincula o banco (item) da conta nº `indice` da lista de 'contas'. Não apaga lançamentos."""
    contas = ler_contas(user_phone)
    if not contas or indice < 1 or indice > len(contas):
        return "⚠️ Não achei essa conta. Digite *contas* para ver a lista numerada."
    alvo = contas[indice - 1]
    item_id, key = alvo.get("item_id"), get_lookup_prefix(user_phone)
    if not item_id:
        return "⚠️ Essa conta não tem banco vinculado."
    do_banco = [c for c in contas if c.get("item_id") == item_id]
    cli = db.get_db()
    for c in do_banco:
        cli.table(T_FATURAS).delete().eq("user_key", key).eq("account_id", c["id"]).execute()
        cli.table(T_CONTAS).delete().eq("user_key", key).eq("account_id", c["id"]).execute()
    cli.table(itens_mod.TABELA).delete().eq("user_key", key).eq("item_id", item_id).execute()
    return ("✅ Banco removido:\n" + "\n".join(f"• {c['nome']}" for c in do_banco) +
            "\n\nSeus lançamentos já importados continuam guardados.")


MSG_CONECTAR_BANCO = (
    "🏦 *Para conectar um banco:*\n"
    "1. Conecte o banco na Pluggy (meu.pluggy.ai ou o painel da sua aplicação)\n"
    "2. Abra o item conectado e copie o *código do item*\n"
    "3. Me mande: *adicionar banco <código>*\n\n"
    "Depois é só digitar *saldo* ou *sincronizar*.\n"
    "_(Em breve: um link para conectar direto por aqui.)_"
)


# ── previsão das próximas faturas ────────────────────────────────────────────
_MESES_LONGOS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def formatar_faturas_futuras(meses: list[tuple[date, list[dict]]]) -> str:
    """meses: [(1º dia do mês, [{nome, vence, lancado, previsto, banco}])]. `banco` (float) = total já fechado pelo banco."""
    if not meses:
        return "📅 Não encontrei faturas futuras para mostrar."
    linhas = ["📅 *Faturas futuras* (previsão)"]
    geral = 0.0
    for mes, cartoes in meses:
        linhas.append(f"\n*{_MESES_LONGOS[mes.month - 1].capitalize()}/{mes.year}*")
        soma = 0.0
        for c in cartoes:
            if c.get("banco") is not None:
                total, nota = c["banco"], "fechada no banco"
            else:
                total = round(c["lancado"] + c["previsto"], 2)
                nota = f"lançado R$ {fat.fmt_moeda(c['lancado'])} + previsto R$ {fat.fmt_moeda(c['previsto'])}" if c["previsto"] else "lançado até agora"
            soma += total
            linhas.append(f"💳 {c['nome']} · vence {c['vence'].strftime('%d/%m')}: *R$ {fat.fmt_moeda(total)}* _({nota})_")
        if len(cartoes) > 1:
            linhas.append(f"🧾 Total do mês: *R$ {fat.fmt_moeda(round(soma, 2))}*")
        geral += soma
    linhas.append(f"\n💰 Total no período: *R$ {fat.fmt_moeda(round(geral, 2))}*")
    linhas.append("🔎 Para ver as linhas de uma fatura: *fatura c6 dezembro* (use o nome do cartão e o mês).")
    linhas.append("_Valores com “previsto” são estimativas: só incluem parcelas já conhecidas, não compras novas nem assinaturas._")
    return "\n".join(linhas)


def faturas_futuras(user_phone: str, quantos: int = 6) -> str:
    cartoes = [c for c in ler_contas(user_phone) if c["tipo"] == "CREDIT"]
    if not cartoes:
        return "💳 Não achei cartões de crédito conectados."
    return formatar_faturas_futuras(sorted(_faturas_projetadas(user_phone, quantos).items())[:quantos])


def _faturas_projetadas(user_phone: str, quantos: int = 6) -> dict[date, list[dict]]:
    """{1º dia do mês: [{nome, vence, lancado, previsto, banco}]} das faturas ainda a vencer (compartilhado por
    'faturas futuras' e 'fluxo')."""
    hoje = hoje_local()
    cartoes = [c for c in ler_contas(user_phone) if c["tipo"] == "CREDIT"]
    try:
        parcelas = db.get_active_installments(user_phone)
    except Exception as e:
        logger.warning(f"Parcelas indisponíveis para a previsão: {e}")
        parcelas = []
    por_mes: dict[date, list[dict]] = {}
    for c in cartoes:
        ciclos = [ci for ci, _ in ler_faturas(user_phone, c["id"])]
        meus = [x for x in parcelas if x.get("account_id") == c["id"]]
        base = date(hoje.year, hoje.month, 1)
        for i in range(quantos + 1):
            mes = parcelas_proj._soma_meses(base, i)
            due = vencimento_do_mes(user_phone, mes.year, mes.month, c["id"])
            if due is None or due < hoje:
                continue
            gastos = [g for g in _gastos_da_fatura(user_phone, mes) if g.get("account_id") == c["id"]]
            creds = [x for x in _creditos_da_fatura(user_phone, mes) if x.get("account_id") == c["id"]]
            lancado = round(sum(float(g["amount"]) for g in gastos) - sum(abs(float(x["amount"])) for x in creds), 2)
            prev, _n = parcelas_proj.previstas_do_mes(meus, mes, hoje)
            banco = None
            ci = next((k for k in ciclos if (k.vencimento.year, k.vencimento.month) == (mes.year, mes.month)), None)
            if ci is not None and not ci.estimado and ci.total is not None and ci.fechamento < hoje:
                banco = ci.total
            por_mes.setdefault(mes, []).append({"nome": c["nome"], "vence": due, "lancado": lancado, "previsto": prev, "banco": banco})
    return por_mes


def _receitas_recentes(user_phone: str, hoje: date, dias: int = 150) -> list[dict]:
    """Entradas (não de cartão) dos últimos meses, para detectar salário e outras recorrências."""
    try:
        ini = (hoje - timedelta(days=dias)).isoformat()
        res = (db.get_db().table("finbot_expenses")
               .select("description, amount, category, subcategory, purchase_date, billing_date, payment_method")
               .ilike("user_phone", db._q(user_phone)).eq("transaction_type", "income")
               .gte("billing_date", ini).execute())
        return [r for r in (res.data or []) if r.get("payment_method") != "credito"]
    except Exception as e:
        logger.warning(f"Receitas indisponíveis para o fluxo de caixa: {e}")
        return []


def _despesas_conta_recentes(user_phone: str, hoje: date, dias: int = 190) -> list[dict]:
    """Gastos pagos pela conta (débito/Pix/boleto), sem cartão. Pagamento de fatura e transferência entre contas
    próprias já vêm como 'transferencia' (movement) e não entram."""
    try:
        ini = (hoje - timedelta(days=dias)).isoformat()
        res = (db.get_db().table("finbot_expenses")
               .select("description, amount, category, purchase_date, billing_date, payment_method")
               .ilike("user_phone", db._q(user_phone)).eq("movement", "expense")
               .gte("purchase_date", ini).execute())
        return [r for r in (res.data or []) if r.get("payment_method") != "credito"]
    except Exception as e:
        logger.warning(f"Despesas de conta indisponíveis para o fluxo de caixa: {e}")
        return []


def fluxo_projecao(user_phone: str, dias: int = 30) -> dict | None:
    """Projeção do saldo das contas correntes dia a dia (None se não há conta corrente com saldo): faturas a vencer,
    contas fixas, entradas recorrentes e o ritmo dos gastos do dia a dia."""
    hoje = hoje_local()
    contas = [c for c in ler_contas(user_phone) if c["tipo"] == "BANK" and c.get("saldo") is not None]
    if not contas:
        return None
    eventos: list[fluxo_mod.Evento] = []
    for itens in _faturas_projetadas(user_phone, 3 + dias // 30).values():
        for c in itens:
            total = c["banco"] if c.get("banco") is not None else round(c["lancado"] + c["previsto"], 2)
            if total > 0:
                eventos.append(fluxo_mod.Evento(c["vence"], f"{c['nome']} (fatura)", -total))
    eventos += fluxo_mod.entradas_recorrentes(_receitas_recentes(user_phone, hoje), hoje, dias)
    despesas = _despesas_conta_recentes(user_phone, hoje)
    fixas, chaves_fixas = fluxo_mod.saidas_recorrentes(despesas, hoje, dias)
    eventos += fixas
    ritmo = fluxo_mod.ritmo_projecao(despesas, chaves_fixas, hoje)   # média, ou ajustada por dia da semana/parte do mês
    saldo = round(sum(c["saldo"] for c in contas), 2)
    return fluxo_mod.projetar(saldo, eventos, hoje, dias, ritmo)


def fluxo_caixa(user_phone: str, dias: int = 30) -> str:
    """O mesmo fluxo, como texto do WhatsApp."""
    proj = fluxo_projecao(user_phone, dias)
    return fluxo_mod.formatar(None, sem_contas=True) if proj is None else fluxo_mod.formatar(proj)


def previsao_dados(user_phone: str):
    """Quanto o mês deve fechar, por categoria: realizado + o que ainda deve sair até o último dia.
    Devolve (linhas, hoje, fim_mes, gasto do mês anterior)."""
    hoje = hoje_local()
    mes_ref = hoje.strftime("%Y-%m")
    fim_mes = date(hoje.year, hoje.month, monthrange(hoje.year, hoje.month)[1])
    restam = (fim_mes - hoje).days
    realizado = {r["category"]: r["total"] for r in db.monthly_by_category(user_phone, mes_ref)}
    despesas = _despesas_conta_recentes(user_phone, hoje)
    fixas, chaves = fluxo_mod.saidas_recorrentes(despesas, hoje, restam)
    restante = {c: v * restam for c, v in previsao_mod.ritmo_por_categoria(despesas, chaves, hoje).items()}
    for e in fixas:
        cat = previsao_mod.categoria_da_conta_fixa(despesas, e.descricao)
        restante[cat] = restante.get(cat, 0.0) + abs(e.valor)
    try:
        for c in _faturas_projetadas(user_phone, 1).get(date(hoje.year, hoje.month, 1), []):
            if c.get("banco") is None and c.get("previsto", 0) > 0:
                restante["Parcelas previstas (cartão)"] = restante.get("Parcelas previstas (cartão)", 0.0) + c["previsto"]
    except Exception as e:
        logger.warning(f"Parcelas previstas indisponíveis para a previsão do mês: {e}")
    limites = {b["category"]: b["amount"] for b in db.get_all_budgets(user_phone, mes_ref)}
    anterior = db.get_resumo_mes_anterior(user_phone).get("gastos") or None
    return previsao_mod.montar(realizado, restante, limites), hoje, fim_mes, anterior


def previsao_mes(user_phone: str) -> str:
    """A mesma previsão, como texto do WhatsApp."""
    return previsao_mod.formatar(*previsao_dados(user_phone))
