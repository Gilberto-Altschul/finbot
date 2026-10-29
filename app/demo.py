"""
Modo demonstração: dados FICTÍCIOS para mostrar o FinBot sem tocar nos dados reais.

Como funciona
- "modo demo" liga: o bot passa a usar um usuário sintético ligado ao mesmo WhatsApp (telefone "whatsapp:+0<número>";
  nenhum número real começa com 0 depois do +, então não há colisão). As respostas continuam indo para o número real.
- "sair do demo" volta aos dados reais (que nunca foram tocados). "limpar demo" apaga só os dados fictícios.
- O modo vale DURACAO_H horas e vive na memória: se o app reiniciar, volta ao real (lado seguro).
- "sair do demo" e a expiração apagam os dados fictícios; "modo demo" gera de novo (datas sempre relativas a hoje).
- O usuário sintético nunca fala com a Pluggy (ver guardas em dados_banco/tools): não vincula nem sincroniza banco.
"""
from __future__ import annotations

import logging
import random
import re
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import app.database as db
from app.utils import _normalize, get_lookup_prefix

logger = logging.getLogger(__name__)

PREFIXO = "whatsapp:+0"
DURACAO_H = 4
_ATIVOS: dict[str, float] = {}   # telefone real -> expira (epoch)

CONTA = {"id": "demo-conta-1", "nome": "Banco Demo Conta", "saldo": 1450.00}
CARTOES = [
    {"id": "demo-cartao-1", "nome": "Banco Demo Gold", "fecha": 20, "vence": 28, "limite": 15000.0},
    {"id": "demo-cartao-2", "nome": "Banco Azul Platinum", "fecha": 5, "vence": 12, "limite": 8000.0},
]
ITEM = "demo-item"
DIA_SALARIO = 5
CONTAS_FIXAS_DIA = [(10, 1850.00), (15, 1240.00), (20, 119.90), (25, 389.00)]
MARGEM_SALDO = 1500.00
MESES_HISTORICO = 6


# ── identidade ────────────────────────────────────────────────────────────────
def eh_telefone_demo(telefone: str | None) -> bool:
    return str(telefone or "").startswith(PREFIXO)


def telefone_demo(real: str) -> str:
    digitos = re.sub(r"\D", "", real)
    return f"{PREFIXO}{digitos}"


def telefone_real(demo: str) -> str:
    """whatsapp:+0<número> -> whatsapp:+<número>. Qualquer outro valor volta igual."""
    if not eh_telefone_demo(demo):
        return demo
    return "whatsapp:+" + demo[len(PREFIXO):]


# ── estado (liga/desliga) ─────────────────────────────────────────────────────
def ativar(real: str, agora: float | None = None) -> None:
    _ATIVOS[real] = (agora if agora is not None else time.time()) + DURACAO_H * 3600


def desativar(real: str) -> None:
    _ATIVOS.pop(real, None)


def ativo(real: str, agora: float | None = None) -> bool:
    exp = _ATIVOS.get(real)
    if exp is None:
        return False
    if (agora if agora is not None else time.time()) >= exp:
        _ATIVOS.pop(real, None)
        return False
    return True


def limpar_se_expirou(real: str, agora: float | None = None) -> bool:
    """Se o modo demo de `real` passou do prazo, desliga E apaga os dados fictícios. True se apagou."""
    exp = _ATIVOS.get(real)
    if exp is None or (agora if agora is not None else time.time()) < exp:
        return False
    _ATIVOS.pop(real, None)
    try:
        limpar(telefone_demo(real))
    except Exception as e:
        logger.error(f"Modo demo: falha ao apagar dados expirados: {e}")
    return True


def efetivo(real: str) -> str:
    """Telefone a usar nos dados: o sintético se o modo demo está ligado; senão o próprio."""
    return telefone_demo(real) if ativo(real) else real


def comando(texto: str | None) -> str | None:
    t = _normalize(texto or "").strip(" .!")
    if t in ("modo demo", "modo demonstracao", "ativar demo", "ligar demo", "demo"):
        return "ativar"
    if t in ("sair do demo", "sair demo", "sair do modo demo", "desligar demo", "modo real", "sair da demo"):
        return "sair"
    if t in ("limpar demo", "apagar demo", "resetar demo"):
        return "limpar"
    return None


def executar(real: str, cmd: str) -> str:
    try:
        if cmd == "ativar":
            demo = telefone_demo(real)
            if not _tem_dados(demo):
                gerar(demo)
            ativar(real)
            return ("🎭 *Modo demonstração ligado* (por %dh).\n\nAgora o FinBot mostra dados *fictícios* — seus dados reais "
                    "não são tocados nem misturados.\n\nExperimente: *saldos*, *resumo*, *fatura*, *faturas futuras*, "
                    "*fluxo*, *previsão do mês*, *parcelamentos*.\n\nPara voltar: *sair do demo*. Para apagar os dados "
                    "fictícios: *limpar demo*." % DURACAO_H)
        if cmd == "sair":
            desativar(real)
            limpar(telefone_demo(real))   # não deixa dado fictício parado no banco
            return "✅ Voltei para os seus dados *reais*. Os dados da demonstração foram apagados (digite *modo demo* para gerar de novo)."
        if cmd == "limpar":
            desativar(real)
            limpar(telefone_demo(real))
            return "🧹 Dados da demonstração apagados. Você está nos dados *reais*. Digite *modo demo* para gerar de novo."
    except Exception as e:
        logger.error(f"Modo demo falhou ({cmd}): {e}")
        return "⚠️ Não consegui executar o modo demonstração agora. Seus dados reais continuam intactos."
    return ""


# ── dados fictícios ───────────────────────────────────────────────────────────
def _tem_dados(demo: str) -> bool:
    try:
        return bool(db.get_db().table("finbot_contas").select("account_id")
                    .eq("user_key", get_lookup_prefix(demo)).limit(1).execute().data)
    except Exception:
        return False


def limpar(demo: str) -> None:
    """Apaga TUDO do usuário sintético. Recusa qualquer telefone que não seja sintético."""
    if not eh_telefone_demo(demo):
        raise ValueError("limpar() só apaga usuário de demonstração")
    cli, chave = db.get_db(), get_lookup_prefix(demo)
    for tabela in ("finbot_expenses", "finbot_budgets", "finbot_conversation", "finbot_user_connections"):
        cli.table(tabela).delete().ilike("user_phone", db._q(demo)).execute()
    for tabela in ("finbot_contas", "finbot_faturas", "finbot_saldo_historico", "finbot_user_merchant_stats",
                   "finbot_pluggy_itens"):
        try:
            cli.table(tabela).delete().eq("user_key", chave).execute()
        except Exception as e:
            logger.warning(f"limpar demo: {tabela}: {e}")


def _soma_meses(d: date, n: int) -> date:
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    return date(ano, mes, min(d.day, monthrange(ano, mes)[1]))


def _fatura_do(cartao: dict, d: date) -> tuple[date, date, date]:
    """(1º dia do mês de cobrança, fechamento, vencimento) da fatura onde cai uma compra em `d`."""
    mes = d if d.day <= cartao["fecha"] else _soma_meses(date(d.year, d.month, 1), 1)
    base = date(mes.year, mes.month, 1)
    return base, date(base.year, base.month, cartao["fecha"]), date(base.year, base.month, cartao["vence"])


def montar_linhas(demo: str, hoje: date, seed: int = 7) -> dict:
    """Constrói (sem gravar) lançamentos, contas e faturas fictícios, sempre relativos a `hoje`."""
    rng = random.Random(seed)
    lin: list[dict] = []
    n = [0]

    def base(desc, valor, cat, sub, dia, conta, metodo, tipo="expense", mov=None, **extra):
        n[0] += 1
        r = {"user_phone": demo, "amount": round(valor, 2), "category": cat, "subcategory": sub, "description": desc,
             "pluggy_transaction_id": f"demo-{n[0]:05d}", "transaction_type": tipo, "payment_method": metodo,
             "purchase_date": dia.isoformat(), "billing_date": dia.isoformat(), "is_forecast": False,
             "account_id": conta, "merchant_key": _normalize(desc)[:30], "review_status": "confirmado",
             "confidence": 1.0, "movement": mov or ("income" if tipo == "income" else "expense")}
        r.update(extra)
        return r

    def debito(desc, valor, cat, sub, dia, **kw):
        if dia <= hoje:
            lin.append(base(desc, valor, cat, sub, dia, CONTA["id"], "debito", **kw))

    def cartao(c, desc, valor, cat, sub, dia, parcela=None):
        if dia > hoje:
            return
        mes_cob, fecha, _venc = _fatura_do(c, dia)
        extra = {"billing_date": mes_cob.isoformat(), "is_forecast": fecha >= hoje}
        if parcela:
            extra.update(installment_of=parcela[0], installment_total=parcela[1])
        lin.append(base(desc, valor, cat, sub, dia, c["id"], "credito", **extra))

    inicio = _soma_meses(date(hoje.year, hoje.month, 1), -(MESES_HISTORICO - 1))
    c1, c2 = CARTOES
    for k in range(MESES_HISTORICO):
        m0 = _soma_meses(inicio, k)
        D = lambda dia: date(m0.year, m0.month, min(dia, monthrange(m0.year, m0.month)[1]))  # noqa: E731
        # entradas e contas fixas (conta corrente)
        debito("Salario Empresa Demo Ltda", 11500.00, "Receitas", "Salário e Pró-labore", D(5), tipo="income")
        if k == 2:
            debito("Pix recebido Cliente Freela Demo", 1800.00, "Receitas", "Freelance e Bônus", D(18), tipo="income")
        debito("Pagamento de boleto COND RESIDENCIAL DEMO", 1850.00, "Moradia", "Condomínio", D(10))
        debito("Pagamento de boleto PLANO SAUDE DEMO", 1240.00, "Saúde", "Plano de Saúde", D(15))
        debito("Debito automatico INTERNET FIBRA DEMO", 119.90, "Moradia", "Internet e TV", D(20))
        debito("Pagamento de boleto SEGURO AUTO DEMO", 389.00, "Seguros", "Seguro Auto", D(25))
        # gastos variáveis de débito/Pix
        for _ in range(4):
            debito("Compra debito Mercado Demo", rng.uniform(120, 380), "Alimentação", "Mercado", D(rng.randint(1, 28)))
        for _ in range(6):
            debito("Compra debito Padaria Demo", rng.uniform(12, 45), "Alimentação", "Padaria e Café", D(rng.randint(1, 28)))
        for _ in range(3):
            debito("Compra debito Restaurante Demo", rng.uniform(60, 140), "Alimentação", "Restaurante", D(rng.randint(1, 28)))
        for _ in range(2):
            debito("Compra debito Farmacia Demo", rng.uniform(40, 160), "Saúde", "Farmácia e Suplementos", D(rng.randint(1, 28)))
        for _ in range(2):
            debito("Compra debito Posto Demo", rng.uniform(150, 240), "Transporte", "Combustível", D(rng.randint(1, 28)))
        for _ in range(2):
            debito("Pix enviado Maria Demo", rng.uniform(150, 300), "Família e Dependentes", "Apoio Familiar", D(rng.randint(1, 28)))
        debito("Compra debito Barbearia Demo", 70.00, "Vestuário e Beleza", "Cabeleireiro e Barbearia", D(rng.randint(1, 28)))
        # cartão 1
        cartao(c1, "Netflix Demo", 55.90, "Lazer e Cultura", "Streaming e Assinaturas", D(8))
        cartao(c1, "Spotify Demo", 21.90, "Lazer e Cultura", "Streaming e Assinaturas", D(12))
        for _ in range(4):
            cartao(c1, "iFood Demo", rng.uniform(35, 90), "Alimentação", "Delivery", D(rng.randint(1, 28)))
        for _ in range(5):
            cartao(c1, "Uber Demo", rng.uniform(14, 38), "Transporte", "Aplicativo e Táxi", D(rng.randint(1, 28)))
        cartao(c1, "Amazon Marketplace Demo", rng.uniform(80, 250), "Outros", "Compras Online", D(rng.randint(1, 28)))
        cartao(c1, "Loja Roupa Demo", rng.uniform(150, 400), "Vestuário e Beleza", "Roupa", D(rng.randint(1, 28)))
        # cartão 2
        cartao(c2, "Academia Demo Fit", 129.90, "Saúde", "Academia e Esportes", D(3))
        for _ in range(3):
            cartao(c2, "Bar e Restaurante Demo", rng.uniform(80, 220), "Alimentação", "Bar", D(rng.randint(1, 28)))
        cartao(c2, "Cinema Demo", rng.uniform(40, 120), "Lazer e Cultura", "Cinema, Shows e Eventos", D(rng.randint(1, 28)))

    # compras parceladas: cada parcela cai no mês seguinte, no mesmo dia (como no cartão real)
    def parcelado(c, desc, total_parcelas, parcela_valor, cat, sub, meses_atras, dia, primeira_extra=0.0):
        origem = _soma_meses(date(hoje.year, hoje.month, 1), -meses_atras)
        for p in range(1, total_parcelas + 1):
            d = _soma_meses(date(origem.year, origem.month, min(dia, 28)), p - 1)
            valor = parcela_valor + (primeira_extra if p == 1 else 0.0)
            cartao(c, desc, valor, cat, sub, d, parcela=(p, total_parcelas))

    parcelado(c1, "Magazine Demo Notebook", 12, 350.00, "Trabalho e Negócio", "Ferramentas e Software", 3, 14)
    parcelado(c1, "Amazon Mktplc Fone Demo", 3, 125.92, "Outros", "Compras Online", 2, 25, primeira_extra=0.01)
    parcelado(c1, "Loja Viagem Demo", 6, 289.90, "Viagens", "Passagens", 1, 9)
    parcelado(c2, "Eletro Demo Geladeira", 10, 459.90, "Moradia", "Móveis e Utensílios", 5, 6)
    parcelado(c2, "Joalheria Demo", 4, 212.50, "Presentes e Doações", "Presentes", 2, 11)

    # faturas dos cartões: cada ciclo fechado soma suas linhas; pagamento na conta nos ciclos já vencidos
    faturas, contas = [], []
    faturas_a_pagar: list[tuple[date, float]] = []
    agora = datetime.now(timezone.utc).isoformat()
    chave = get_lookup_prefix(demo)
    for c in CARTOES:
        por_ciclo: dict[date, float] = {}
        for r in lin:
            if r["account_id"] == c["id"]:
                b = date.fromisoformat(r["billing_date"])
                por_ciclo[b] = por_ciclo.get(b, 0.0) + r["amount"]
        devedor = 0.0
        primeiro = _soma_meses(date(hoje.year, hoje.month, 1), -(MESES_HISTORICO - 1))
        for i in range(MESES_HISTORICO + 2):
            b = _soma_meses(primeiro, i)
            fecha, venc = date(b.year, b.month, c["fecha"]), date(b.year, b.month, c["vence"])
            if fecha > _soma_meses(hoje, 1):
                continue
            fechada = fecha < hoje
            total = round(por_ciclo.get(b, 0.0), 2)
            paga = venc < hoje
            if fechada and not paga:
                devedor += total
                faturas_a_pagar.append((venc, total))
            if not fechada:
                devedor += total
            faturas.append({"user_key": chave, "bill_id": f"demo-{c['id']}-{b:%Y%m}", "account_id": c["id"],
                            "vencimento": venc.isoformat(), "fechamento": fecha.isoformat(),
                            "fechamento_estimado": not fechada, "total": total if fechada else None,
                            "paga": paga, "sincronizado_em": agora})
            if paga and total > 0:
                lin.append(base(f"Pagamento de fatura {c['nome']}", total, "Transferências", "Pagamento de Fatura", venc,
                                CONTA["id"], "debito", mov="transfer"))
        contas.append({"user_key": chave, "account_id": c["id"], "item_id": ITEM, "tipo": "CREDIT", "nome": c["nome"],
                       "banco_nome": c["nome"], "saldo": round(devedor, 2), "credit_limit": c["limite"],
                       "available_limit": round(c["limite"] - devedor, 2), "atualizado_banco": agora,
                       "sincronizado_em": agora})
    contas.insert(0, {"user_key": chave, "account_id": CONTA["id"], "item_id": ITEM, "tipo": "BANK", "nome": CONTA["nome"],
                      "banco_nome": "Banco Demo",
                      "saldo": _saldo_conta(hoje, faturas_a_pagar), "credit_limit": None, "available_limit": None,
                      "atualizado_banco": agora, "sincronizado_em": agora})
    return {"lancamentos": lin, "contas": contas, "faturas": faturas}


def _saldo_conta(hoje: date, faturas_a_pagar: list[tuple[date, float]]) -> float:
    """Saldo da conta de demonstração coerente com a data: cobre as saídas conhecidas até o próximo salário
    (faturas e contas fixas) e o gasto diário, com uma margem. Assim o fluxo nunca nasce negativo."""
    sal = date(hoje.year, hoje.month, DIA_SALARIO)
    if sal < hoje:
        sal = _soma_meses(date(hoje.year, hoje.month, 1), 1).replace(day=DIA_SALARIO)
    saidas = sum(t for v, t in faturas_a_pagar if hoje <= v < sal)
    for d in (hoje, _soma_meses(date(hoje.year, hoje.month, 1), 1)):
        for dia, valor in CONTAS_FIXAS_DIA:
            dt = date(d.year, d.month, dia)
            if hoje <= dt < sal:
                saidas += valor
    saidas += 100.0 * (sal - hoje).days
    return round((saidas + MARGEM_SALDO) / 10) * 10.0


def gerar(demo: str, hoje: date | None = None) -> dict:
    """Grava os dados fictícios do usuário sintético (apaga os anteriores dele antes: pode rodar de novo)."""
    if not eh_telefone_demo(demo):
        raise ValueError("gerar() só cria dados para usuário de demonstração")
    if hoje is None:
        from app.dados_banco import hoje_local
        hoje = hoje_local()
    limpar(demo)
    dados = montar_linhas(demo, hoje)
    db._get_or_create_user_connection(demo)
    db.save_user_settings(demo, CARTOES[0]["vence"], CARTOES[0]["fecha"])
    cli = db.get_db()
    cli.table("finbot_contas").upsert(dados["contas"], on_conflict="user_key,account_id").execute()
    cli.table("finbot_faturas").upsert(dados["faturas"], on_conflict="user_key,bill_id").execute()
    gravados = db.inserir_gastos_em_lote(dados["lancamentos"])
    mes_ref = hoje.strftime("%Y-%m")
    for cat, valor in (("Alimentação", 1800.0), ("Transporte", 700.0), ("Lazer e Cultura", 400.0),
                       ("Saúde", 600.0), ("Vestuário e Beleza", 500.0)):
        db.save_budget(demo, cat, valor, mes_ref)
    logger.info(f"Modo demo: {gravados} lançamentos, {len(dados['contas'])} contas, {len(dados['faturas'])} faturas.")
    return {"lancamentos": gravados, "contas": len(dados["contas"]), "faturas": len(dados["faturas"])}
