# app/ingestion.py
import logging
import json
from datetime import date
import app.database as db
import asyncio
from dataclasses import dataclass
import app.revisao as revisao
import app.faturas as _fat
from app.classificador import classificar
from app.merchant import normalizar_merchant, familia_ambigua

logger = logging.getLogger(__name__)

# Categorias válidas para o usuário escolher
from app.taxonomia import CATEGORIAS_DESPESA, SUBCATEGORIAS, A_CLASSIFICAR, traduzir
CATEGORIAS_VALIDAS = CATEGORIAS_DESPESA   # taxonomia v2

# Mapa de subcategorias → (categoria, subcategoria)
# O usuário digita palavras do dia a dia e o sistema resolve a hierarquia
SUBCATEGORIA_MAP = {
    # Alimentação
    "mercado": ("Alimentação", "Mercado"),
    "supermercado": ("Alimentação", "Mercado"),
    "restaurante": ("Alimentação", "Restaurante"),
    "lanche": ("Alimentação", "Restaurante"),
    "padaria": ("Alimentação", "Padaria"),
    "delivery": ("Alimentação", "Delivery"),
    "ifood": ("Alimentação", "Delivery"),
    "cafe": ("Alimentação", "Café"),
    "café": ("Alimentação", "Café"),
    "comida": ("Alimentação", "Restaurante"),
    "hortifruti": ("Alimentação", "Feira e Hortifruti"),
    "feira": ("Alimentação", "Feira e Hortifruti"),
    "acougue": ("Alimentação", "Feira e Hortifruti"),
    "açougue": ("Alimentação", "Feira e Hortifruti"),
    # Transporte
    "uber": ("Transporte", "Aplicativo"),
    "99": ("Transporte", "Aplicativo"),
    "taxi": ("Transporte", "Aplicativo"),
    "táxi": ("Transporte", "Aplicativo"),
    "onibus": ("Transporte", "Transporte Público"),
    "ônibus": ("Transporte", "Transporte Público"),
    "metro": ("Transporte", "Transporte Público"),
    "metrô": ("Transporte", "Transporte Público"),
    "gasolina": ("Transporte", "Combustível"),
    "combustivel": ("Transporte", "Combustível"),
    "combustível": ("Transporte", "Combustível"),
    "estacionamento": ("Transporte", "Estacionamento"),
    "pedagio": ("Transporte", "Estacionamento"),
    "pedágio": ("Transporte", "Estacionamento"),
    "manutencao": ("Transporte", "Oficina"),
    "manutenção": ("Transporte", "Oficina"),
    "oficina": ("Transporte", "Oficina"),
    "lava rapido": ("Transporte", "Oficina"),
    "lava rápido": ("Transporte", "Oficina"),
    "lava jato": ("Transporte", "Oficina"),
    "lavagem": ("Transporte", "Oficina"),
    "mecanico": ("Transporte", "Oficina"),
    "mecânico": ("Transporte", "Oficina"),
    "ipva": ("Transporte", "IPVA"),
    "seguro auto": ("Transporte", "Seguro Auto"),
    "seguro carro": ("Transporte", "Seguro Auto"),
    # Lazer
    "cinema": ("Lazer", "Cinema e Shows"),
    "show": ("Lazer", "Cinema e Shows"),
    "teatro": ("Lazer", "Cinema e Shows"),
    "streaming": ("Lazer", "Streaming"),
    "netflix": ("Lazer", "Streaming"),
    "spotify": ("Lazer", "Streaming"),
    "jogo": ("Lazer", "Hobbies e Jogos"),
    "games": ("Lazer", "Hobbies e Jogos"),
    "hobby": ("Lazer", "Hobbies e Jogos"),
    "parque": ("Lazer", "Hobbies e Jogos"),
    "viagem": ("Lazer", "Viagem"),
    "hotel": ("Lazer", "Viagem"),
    "hospedagem": ("Lazer", "Viagem"),
    "restaurante lazer": ("Lazer", "Restaurante lazer"),
    "jantar": ("Lazer", "Restaurante lazer"),
    "jantar fora": ("Lazer", "Restaurante lazer"),
    "bar": ("Lazer", "Bar"),
    "balada": ("Lazer", "Bar"),
    "diversao": ("Lazer", "Hobbies e Jogos"),
    "diversão": ("Lazer", "Hobbies e Jogos"),
    "entretenimento": ("Lazer", "Hobbies e Jogos"),
    "presente": ("Presentes e Doações", "Presentes"),
    "presentes": ("Presentes e Doações", "Presentes"),
    "gift": ("Presentes e Doações", "Presentes"),
    "doacao": ("Presentes e Doações", "Doações"),
    "doação": ("Presentes e Doações", "Doações"),
    "doacoes": ("Presentes e Doações", "Doações"),
    "doações": ("Presentes e Doações", "Doações"),
    "caridade": ("Presentes e Doações", "Doações"),
    "vaquinha": ("Presentes e Doações", "Doações"),
    "dizimo": ("Presentes e Doações", "Dízimo e Ofertas"),
    "dízimo": ("Presentes e Doações", "Dízimo e Ofertas"),
    "igreja": ("Presentes e Doações", "Dízimo e Ofertas"),
    # Moradia
    "aluguel": ("Moradia", "Aluguel"),
    "condominio": ("Moradia", "Condomínio"),
    "condomínio": ("Moradia", "Condomínio"),
    "luz": ("Moradia", "Contas"),
    "energia": ("Moradia", "Contas"),
    "agua": ("Moradia", "Contas"),
    "água": ("Moradia", "Contas"),
    "gas": ("Moradia", "Contas"),
    "gás": ("Moradia", "Contas"),
    "internet": ("Moradia", "Internet e TV"),
    "tv": ("Moradia", "Internet e TV"),
    "telefone": ("Moradia", "Celular"),
    "celular": ("Moradia", "Celular"),
    "reforma": ("Moradia", "Reforma e Manutenção"),
    "mobilia": ("Moradia", "Utensílos"),
    "mobília": ("Moradia", "Utensílos"),
    "utensilio": ("Moradia", "Utensílos"),
    "utensílio": ("Moradia", "Utensílos"),
    "faxina": ("Moradia", "Faxina"),
    "diarista": ("Moradia", "Faxina"),
    # Saúde
    "farmacia": ("Saúde", "Farmácia"),
    "farmácia": ("Saúde", "Farmácia"),
    "remedio": ("Saúde", "Farmácia"),
    "remédio": ("Saúde", "Farmácia"),
    "suplemento": ("Saúde", "Suplemento"),
    "suplementos": ("Saúde", "Suplemento"),
    "vitamina": ("Saúde", "Suplemento"),
    "whey": ("Saúde", "Suplemento"),
    "proteina": ("Saúde", "Suplemento"),
    "proteína": ("Saúde", "Suplemento"),
    "medico": ("Saúde", "Médico"),
    "médico": ("Saúde", "Médico"),
    "consulta": ("Saúde", "Médico"),
    "dentista": ("Saúde", "Médico"),
    "exame": ("Saúde", "Médico"),
    "hospital": ("Saúde", "Médico"),
    "plano": ("Saúde", "Plano de Saúde"),
    "convenio": ("Saúde", "Plano de Saúde"),
    "convênio": ("Saúde", "Plano de Saúde"),
    "academia": ("Saúde", "Academia"),
    "gympass": ("Saúde", "Academia"),
    # Vestuário e Beleza
    "roupa": ("Vestuário e Beleza", "Roupa"),
    "roupas": ("Vestuário e Beleza", "Roupa"),
    "moda": ("Vestuário e Beleza", "Roupa"),
    "vestuario": ("Vestuário e Beleza", "Roupa"),
    "vestuário": ("Vestuário e Beleza", "Roupa"),
    "bolsa": ("Vestuário e Beleza", "Roupa"),
    "acessorio": ("Vestuário e Beleza", "Roupa"),
    "acessório": ("Vestuário e Beleza", "Roupa"),
    "calcado": ("Vestuário e Beleza", "Calçado"),
    "calçado": ("Vestuário e Beleza", "Calçado"),
    "tenis": ("Vestuário e Beleza", "Calçado"),
    "tênis": ("Vestuário e Beleza", "Calçado"),
    "salao": ("Vestuário e Beleza", "Cabeleireiro"),
    "salão": ("Vestuário e Beleza", "Cabeleireiro"),
    "cabelo": ("Vestuário e Beleza", "Cabeleireiro"),
    "barbearia": ("Vestuário e Beleza", "Cabeleireiro"),
    "manicure": ("Vestuário e Beleza", "Manicure"),
    "unha": ("Vestuário e Beleza", "Manicure"),
    "cosmetico": ("Vestuário e Beleza", "Cosméticos"),
    "cosmético": ("Vestuário e Beleza", "Cosméticos"),
    "perfume": ("Vestuário e Beleza", "Cosméticos"),
    "maquiagem": ("Vestuário e Beleza", "Cosméticos"),
    # Educação
    "escola": ("Educação", "Escola e Faculdade"),
    "colegio": ("Educação", "Escola e Faculdade"),
    "colégio": ("Educação", "Escola e Faculdade"),
    "faculdade": ("Educação", "Escola e Faculdade"),
    "universidade": ("Educação", "Escola e Faculdade"),
    "curso": ("Educação", "Curso Online"),
    "livro": ("Educação", "Material Escolar"),
    "livros": ("Educação", "Material Escolar"),
    "material": ("Educação", "Material Escolar"),
    "ingles": ("Educação", "Idiomas"),
    "inglês": ("Educação", "Idiomas"),
    "idioma": ("Educação", "Idiomas"),
    # Pets
    "pet": ("Pets", "Ração"),
    "animal": ("Pets", "Ração"),
    "veterinario": ("Pets", "Veterinário"),
    "veterinário": ("Pets", "Veterinário"),
    "racao": ("Pets", "Ração"),
    "ração": ("Pets", "Ração"),
    "petshop": ("Pets", "Banho e Tosa"),
    "banho": ("Pets", "Banho e Tosa"),
    "tosa": ("Pets", "Banho e Tosa"),
    # Financeiro
    "seguro": ("Financeiro", "Seguro"),
    "emprestimo": ("Financeiro", "Empréstimo e Financiamento"),
    "empréstimo": ("Financeiro", "Empréstimo e Financiamento"),
    "financiamento": ("Financeiro", "Empréstimo e Financiamento"),
    "tarifa": ("Financeiro", "Tarifa"),
    "banco": ("Financeiro", "Tarifa"),
    "juros": ("Financeiro", "Tarifa"),
    # Empresa
    "das": ("Empresa", "Impostos PJ"),
    "imposto": ("Empresa", "Impostos PJ"),
    "contador": ("Empresa", "Contabilidade"),
    "contabilidade": ("Empresa", "Contabilidade"),
    "software": ("Empresa", "Ferramentas e Software"),
    "saas": ("Empresa", "Ferramentas e Software"),
    # Outros — mantido como sentinela: sinaliza "não sei", cai no fluxo de
    # confirmação manual (não corresponde a nenhuma categoria/subcategoria real).
    "outros": ("Outros", "Geral"),
    "geral": ("Outros", "Geral"),
}


def _levenshtein(a: str, b: str) -> int:
    """Calcula a distância de Levenshtein entre duas strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    previous_row = range(len(b) + 1)
    for i, ca in enumerate(a):
        current_row = [i + 1]
        for j, cb in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (ca != cb)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


# Palavras de dia a dia que o usuário digita (e as que vinham da taxonomia antiga) -> nome oficial da taxonomia v2.
SUBCATEGORIA_MAP = {palavra: traduzir(*par) for palavra, par in SUBCATEGORIA_MAP.items()}
# Palavras cuja tradução automática do par antigo seria genérica demais, e palavras que só existem na v2.
SUBCATEGORIA_MAP.update({
    "imposto": ("Impostos", "Outros Impostos"), "ipva": ("Impostos", "IPVA"), "iptu": ("Impostos", "IPTU"),
    "hotel": ("Viagens", "Hospedagem"), "hospedagem": ("Viagens", "Hospedagem"), "viagem": ("Viagens", "Outros em Viagem"),
    "passagem": ("Viagens", "Passagens"), "cambio": ("Viagens", "Câmbio e Remessas"), "wise": ("Viagens", "Câmbio e Remessas"),
    "juros": ("Tarifas e Juros", "Juros e Multas"), "multa": ("Tarifas e Juros", "Juros e Multas"), "iof": ("Tarifas e Juros", "IOF e Câmbio"),
    "emprestimo": ("Dívidas e Financiamentos", "Empréstimos"), "empréstimo": ("Dívidas e Financiamentos", "Empréstimos"),
    "financiamento": ("Dívidas e Financiamentos", "Empréstimos"),
    "delivery": ("Alimentação", "Delivery"), "ifood": ("Alimentação", "Delivery"),
    "marketplace": ("Outros", "Compras Online"), "mercado livre": ("Outros", "Compras Online"), "amazon": ("Outros", "Compras Online"),
})

_POR_NOME: dict[str, tuple[str, str]] = {}


def resolver_exato(texto: str) -> tuple[str, str] | None:
    """Palavra-chave exata OU nome exato de uma subcategoria ("Ferramentas e Software"), sem fuzzy."""
    import unicodedata

    def _normalize(x: str) -> str:   # minúsculas, sem acento, espaços simples
        x = unicodedata.normalize("NFKD", x or "").encode("ascii", "ignore").decode()
        return " ".join(x.lower().split())

    t = (texto or "").strip().lower()
    achado = SUBCATEGORIA_MAP.get(t)
    if achado:
        return achado
    if not _POR_NOME:
        for cat, subs in SUBCATEGORIAS.items():   # nome exato de qualquer subcategoria da taxonomia v2
            for sub in subs:
                _POR_NOME.setdefault(_normalize(sub), (cat, sub))
    return _POR_NOME.get(_normalize(t))


def _resolver_categoria(subcategoria_input: str) -> tuple[str, str] | None:
    """
    Resolve (categoria, subcategoria) a partir de uma palavra do usuário.
    1. Tenta match exato no mapa de subcategorias.
    2. Se não encontrar, tenta fuzzy match (tolerante a typos pequenos).
    Retorna None se não reconhecido.
    """
    inp = subcategoria_input.strip().lower()

    # Match exato (palavra-chave ou nome da subcategoria)
    exato = resolver_exato(inp)
    if exato:
        return exato

    # Fuzzy match: aceita até 1 erro de digitação para palavras curtas (<=6 chars)
    # e até 2 erros para palavras mais longas
    max_dist = 1 if len(inp) <= 6 else 2
    melhor_match = None
    melhor_dist = max_dist + 1

    for chave in SUBCATEGORIA_MAP:
        dist = _levenshtein(inp, chave)
        if dist <= max_dist and dist < melhor_dist:
            melhor_dist = dist
            melhor_match = chave

    if melhor_match:
        logger.info(f"Fuzzy match: '{inp}' → '{melhor_match}' (distância {melhor_dist})")
        return SUBCATEGORIA_MAP[melhor_match]

    return None


def _montar_diagnostico(total: int, novos: int, ignorados: int, auto: int, revisar: int, faturas: int = 0) -> str:
    msg = (
        "🏦 *Extrato processado!*\n\n"
        f"🔍 {total} transações no arquivo.\n"
        f"✨ Importei *{novos} novos gastos* ({auto} já classificados sozinhos).\n"
    )
    if ignorados:
        msg += f"♻️ {ignorados} ignorados por já existirem.\n"
    if faturas:
        msg += f"💳 {faturas} pagamento(s) de fatura de cartão ignorado(s): o gasto já está nas compras do cartão.\n"
    if revisar:
        msg += f"\n🤔 *{revisar}* precisam de um toque seu — já estão no seu painel como _a revisar_.\n"
    return msg


def _cartoes_com_faturas(user_phone: str) -> list[dict]:
    """Cartões do usuário com as faturas guardadas no Supabase: [{"banco": ..., "bills": [...]}]. Falha = lista vazia."""
    try:
        from app.utils import get_lookup_prefix
        import app.faturas as fat
        chave = get_lookup_prefix(user_phone)
        cli = db.get_db()
        contas = (cli.table("finbot_contas").select("account_id, banco_nome, nome")
                  .eq("user_key", chave).eq("tipo", "CREDIT").execute().data or [])
        if not contas:
            return []
        faturas = cli.table("finbot_faturas").select("*").eq("user_key", chave).execute().data or []
        saida = []
        for c in contas:
            mine = [f for f in faturas if f.get("account_id") == c["account_id"]]
            saida.append({"banco": c.get("banco_nome") or c.get("nome") or "", "bills": fat.bills_das_faturas_guardadas(mine)})
        logger.info(f"PDF: {len(saida)} cartão(ões) com {sum(len(x['bills']) for x in saida)} fatura(s) guardada(s) para conferir pagamento de fatura.")
        return saida
    except Exception as e:
        logger.warning(f"Faturas guardadas indisponíveis para checar pagamento de fatura ({e}).")
        return []


def _somas_de_faturas_importadas(user_phone: str) -> list[tuple[date, float]]:
    """Total das compras de cartão já importadas, por mês de cobrança (cliente que usa só PDF). Falha = lista vazia."""
    try:
        from app.utils import get_lookup_prefix
        linhas = (db.get_db().table("finbot_expenses").select("amount, billing_date, transaction_type")
                  .ilike("user_phone", f"{get_lookup_prefix(user_phone)}:%").eq("payment_method", "credito")
                  .limit(5000).execute().data or [])
        por_mes: dict[date, float] = {}
        for r in linhas:
            try:
                d = date.fromisoformat(str(r.get("billing_date"))[:10]).replace(day=1)
            except Exception:
                continue
            v = float(r.get("amount") or 0)
            por_mes[d] = round(por_mes.get(d, 0.0) + (-v if str(r.get("transaction_type")) == "income" else v), 2)
        return sorted(por_mes.items())
    except Exception as e:
        logger.warning(f"Compras de cartão importadas indisponíveis para checar pagamento de fatura ({e}).")
        return []


def _diagnosticar_fatura(tx, dia: date, cartoes: list[dict]) -> None:
    """Só log: quando a saída parece pagar fatura (cita o banco de um cartão) mas não bateu, mostra por quê."""
    from app.utils import _normalize
    palavras = set(_normalize(tx.description or "").split())
    if not cartoes:
        if palavras & {"boleto", "fatura", "cartao"} or "pag" in palavras:
            logger.info(f"Pagamento de fatura? '{tx.description}' R$ {abs(tx.amount):.2f} em {dia}: "
                        f"sem cartões/faturas guardados para conferir (rode 'sincronizar').")
        return
    for c in cartoes:
        if not (_fat._palavras_banco(c.get("banco", "")) & palavras):
            continue
        ciclos = [(str(x.vencimento), x.total) for x in _fat.montar_ciclos(c.get("bills"))]
        proximos = sorted(ciclos, key=lambda t: abs((date.fromisoformat(t[0]) - dia).days))[:3]
        logger.info(f"Pagamento de fatura? '{tx.description}' R$ {abs(tx.amount):.2f} em {dia} cita o cartão "
                    f"'{c.get('banco')}', mas nenhuma fatura bate. Faturas mais próximas (vencimento, total): {proximos}")


@dataclass
class ResultadoImportacao:
    total: int = 0
    inseridos: int = 0
    ignorados: int = 0
    auto: int = 0
    revisar: int = 0
    faturas: int = 0           # pagamentos de fatura de cartão ignorados (o gasto já está nas compras do cartão)
    aviso: str | None = None   # mensagem pronta quando nada foi importado (vazio, já sincronizado, erro)
    erro: bool = False


CLASSIFICAR_EM_PARALELO = 5   # chamadas de classificação (IA + busca na web) simultâneas


async def importar_lote(user_phone: str, all_transactions: list, progresso=None) -> ResultadoImportacao:
    """
    Importa TUDO imediatamente, sem enviar nada ao usuário (serve PDF e Pluggy).
    Cada transação entra com review_status ('auto' ou 'revisar') e confidence.
    """
    total_encontrado = len(all_transactions)
    logger.info(f"Recebidas {total_encontrado} transações para processamento.")
    if not all_transactions:
        return ResultadoImportacao(aviso="✅ Processamento concluído: nenhuma transação foi encontrada no extrato.")

    # 1. Deduplicação por hash/ID (primeira linha de defesa)
    all_tx_ids = list(set(str(tx.id).strip().lower() for tx in all_transactions if tx.id))
    ids_existentes = db.filtrar_transacoes_existentes(user_phone, all_tx_ids)
    logger.info(f"Deduplicação: {len(ids_existentes)} IDs já conhecidos no banco.")

    novas_rows: list[dict] = []
    ids_no_lote: set[str] = set()
    cache_sub_id: dict[str, int | None] = {}

    # Fase 1: separa o que é novo e decide o que precisa de classificação. Mesma padaria 12x no lote = 1 decisão.
    novas: list[tuple] = []          # (tx, tx_id, chave, comum)
    cartoes = None                   # carregado 1x, só se aparecer saída de conta que possa ser pagamento de fatura
    somas = None                     # idem, para quem usa só PDF (sem faturas guardadas)
    faturas_ignoradas = 0
    for tx in all_transactions:
        tx_id = str(tx.id).strip().lower()
        if tx_id in ids_existentes or tx_id in ids_no_lote:
            continue
        # Quitação de fatura pela conta corrente: o gasto já está nas compras do cartão (igual à sincronização Pluggy).
        if tx.type == "expense" and tx.payment_method != "credito":
            if cartoes is None:
                cartoes = _cartoes_com_faturas(user_phone)
            try:
                dia_tx = date.fromisoformat(str(tx.date)[:10])
                if cartoes and _fat.pagamento_de_fatura(tx.description, abs(tx.amount), dia_tx, cartoes):
                    logger.info(f"Ignorando pagamento de fatura do cartão (valor e banco conferem): {tx.description}")
                    faturas_ignoradas += 1
                    ids_no_lote.add(tx_id)
                    continue
                if somas is None:
                    somas = _somas_de_faturas_importadas(user_phone)
                if somas and _fat.pagamento_de_fatura_importada(tx.description, abs(tx.amount), dia_tx, somas):
                    logger.info(f"Ignorando pagamento de fatura (valor igual ao total das compras de cartão importadas): {tx.description}")
                    faturas_ignoradas += 1
                    ids_no_lote.add(tx_id)
                    continue
                _diagnosticar_fatura(tx, dia_tx, cartoes)
            except Exception as e:
                logger.warning(f"Não consegui checar se '{tx.description}' quita fatura ({e}); mantendo o lançamento.")
        ids_no_lote.add(tx_id)
        novas.append((tx, tx_id, normalizar_merchant(tx.description), familia_ambigua(tx.description) is None))

    tarefas: dict = {}               # chave da tarefa -> tx que representa a decisão
    for i, (tx, _tid, chave, comum) in enumerate(novas):
        k = chave if comum else f"#{i}"
        if k not in tarefas:
            tarefas[k] = tx

    # Fase 2: classifica em paralelo (limitado), com aviso de andamento para lotes grandes.
    decisoes: dict = {}
    sem = asyncio.Semaphore(CLASSIFICAR_EM_PARALELO)
    total_tarefas = len(tarefas)
    feitas = 0
    avisos = {0, total_tarefas // 2} if total_tarefas >= 12 else set()

    async def _avisar(n_feitas: int):
        if progresso and n_feitas in avisos:
            try:
                await progresso(n_feitas, total_tarefas, len(novas))
            except Exception as e:      # aviso de andamento nunca pode derrubar a importação
                logger.warning(f"Aviso de andamento falhou: {e}")

    async def _uma(k: str, tx):
        nonlocal feitas
        fallback = traduzir(tx.category, tx.subcategory) if tx.category and tx.category != "Outros" else None   # PDF/IA -> taxonomia v2
        async with sem:
            decisoes[k] = await classificar(user_phone, tx.description, fallback=fallback,
                                            valor=tx.amount, data=str(tx.date))
        feitas += 1
        await _avisar(feitas)

    await _avisar(0)
    await asyncio.gather(*(_uma(k, tx) for k, tx in tarefas.items()))

    # Fase 3: monta as linhas na ordem original.
    for i, (tx, tx_id, chave, comum) in enumerate(novas):
        dec = decisoes[chave if comum else f"#{i}"]

        if dec.subcategoria not in cache_sub_id:
            cache_sub_id[dec.subcategoria] = (
                db.get_subcategory_id_by_name(dec.subcategoria) if dec.subcategoria != "Outros" else None)

        # Receita não entra na fila de perguntas (a revisão só trata despesas)
        status = "auto" if tx.type == "income" else dec.status

        novas_rows.append({
            "pluggy_transaction_id": tx_id,
            "user_phone": user_phone,
            "amount": abs(tx.amount),
            "category": dec.categoria,
            "subcategory": dec.subcategoria,
            "subcategory_id": cache_sub_id[dec.subcategoria],
            "merchant_key": dec.merchant_key,
            "review_status": status,
            "confidence": dec.confidence,
            "description": tx.description,
            "transaction_type": tx.type,
            "payment_method": tx.payment_method,
            "installment_of": tx.installment_of,
            "installment_total": tx.installment_total,
            "purchase_date": tx.date,
            "billing_date": getattr(tx, "billing_date", None) or tx.date,
        })

    if not novas_rows:
        if faturas_ignoradas and not ids_existentes:
            return ResultadoImportacao(
                total=total_encontrado, faturas=faturas_ignoradas,
                aviso=f"💳 Só encontrei pagamento(s) de fatura de cartão ({faturas_ignoradas}); o gasto já está nas compras do cartão.")
        return ResultadoImportacao(
            total=total_encontrado, ignorados=len(ids_existentes),
            aviso=("✅ Seu extrato já estava sincronizado!\n\n"
                   f"🔍 Encontrei {total_encontrado} transações, mas todas já constavam no seu histórico."))

    n_revisar = sum(1 for r in novas_rows if r["review_status"] == "revisar")
    n_auto = len(novas_rows) - n_revisar

    # inserir_gastos_em_lote criptografa user_phone nas rows: contar ANTES.
    inseridos = db.inserir_gastos_em_lote(novas_rows)
    if inseridos == -1:
        return ResultadoImportacao(total=total_encontrado, erro=True,
                                   aviso="❌ Tive um problema ao salvar suas transações. Por favor, tente novamente.")

    return ResultadoImportacao(
        total=total_encontrado, inseridos=inseridos,
        ignorados=len(ids_existentes) + (len(novas_rows) - inseridos),
        auto=n_auto, revisar=n_revisar, faturas=faturas_ignoradas)


async def processar_ingestion_unificada(user_phone: str, all_transactions: list, progresso=None) -> tuple[str, None]:
    """Fluxo do PDF (usuário presente): importa tudo e já abre a primeira pergunta da fila.
    Retorna (mensagem, None) — o segundo item existe só por compatibilidade com main.py."""
    try:
        r = await importar_lote(user_phone, all_transactions, progresso)
        if r.aviso:
            return r.aviso, None
        mensagem = _montar_diagnostico(r.total, r.inseridos, r.ignorados, r.auto, r.revisar, r.faturas)
        if r.revisar:
            try:
                pergunta = await asyncio.to_thread(revisao.iniciar, user_phone)
            except Exception as e:  # as transações já estão salvas: falha aqui não pode virar "erro de importação"
                logger.error(f"Falha ao montar a primeira pergunta de revisão: {e}", exc_info=True)
                pergunta = None
                mensagem += "\nDigite *revisar* quando quiser conferir essas categorias."
            if pergunta:
                mensagem += "\n" + pergunta
        return mensagem, None
    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}", exc_info=True)
        return "❌ Erro ao processar o formato estruturado das transações.", None
