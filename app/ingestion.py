# app/ingestion.py
import logging
import json
from datetime import date
import app.database as db
from app.categorizer import categorizar_gasto_hibrido

logger = logging.getLogger(__name__)

# Categorias válidas para o usuário escolher
CATEGORIAS_VALIDAS = [
    "Alimentação", "Transporte", "Lazer", "Moradia", "Saúde",
    "Vestuário e Beleza", "Educação", "Pets", "Financeiro", "Extra", "Outros"
]

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
    "bar": ("Lazer", "Bar"),
    "balada": ("Lazer", "Bar"),
    "diversao": ("Lazer", "Hobbies e Jogos"),
    "diversão": ("Lazer", "Hobbies e Jogos"),
    "entretenimento": ("Lazer", "Hobbies e Jogos"),
    "presente": ("Lazer", "Presente"),
    "gift": ("Lazer", "Presente"),
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


def _resolver_categoria(subcategoria_input: str) -> tuple[str, str] | None:
    """
    Resolve (categoria, subcategoria) a partir de uma palavra do usuário.
    1. Tenta match exato no mapa de subcategorias.
    2. Se não encontrar, tenta fuzzy match (tolerante a typos pequenos).
    Retorna None se não reconhecido.
    """
    inp = subcategoria_input.strip().lower()

    # Match exato
    exato = SUBCATEGORIA_MAP.get(inp, None)
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


def _montar_mensagem_categorizacao(transacoes_outros: list[dict]) -> str:
    """Monta a mensagem interativa pedindo categorias para transações 'Outros'."""
    n = len(transacoes_outros)
    linhas = [f"🤔 *{n} transações precisam de categoria:*\n"]
    for i, tx in enumerate(transacoes_outros, 1):
        valor = f"R$ {float(tx['amount']):.2f}".replace(".", ",")
        linhas.append(f"*{i}.* {tx['description']} — {valor}")

    linhas.append("\n💡 *Digite a subcategoria com palavras do dia a dia:*")
    linhas.append("roupa · calçado · mercado · restaurante · farmácia · médico")
    linhas.append("gasolina · uber · streaming · aluguel · curso · livro · pet")
    linhas.append("seguro · presente · ou qualquer outra palavra")

    nums = list(range(1, n + 1))
    exemplo_parts = [f"{i} roupa" if i == 1 else f"{i} mercado" if i == 2 else f"{i} farmácia" for i in nums[:3]]
    if n > 3:
        exemplo_parts.append(f"... {n} streaming")
    exemplo = ", ".join(exemplo_parts[:min(n, 4)])

    linhas.append(f"\n✏️ *Responda com TODOS os {n} números em uma única mensagem:*")
    linhas.append(f"Ex: _{exemplo}_")
    linhas.append(f"\n⚠️ Envie tudo de uma vez — não item por item.")
    linhas.append("Ou digite *ok* para salvar tudo como *Outros*.")
    return "\n".join(linhas)


def aplicar_categorizacao_usuario(transactions_json: str, resposta: str) -> list[dict]:
    """
    Aplica as categorias informadas pelo usuário nas transações pendentes.
    Formato esperado: '1 Alimentação, 2 Transporte' ou 'ok'
    """
    transactions = json.loads(transactions_json)

    if resposta.strip().lower() == "ok":
        logger.info("Usuário escolheu salvar tudo como Outros.")
        return transactions

    # Parse da resposta: "1 Alimentação, 2 Saúde, 3 Lazer"
    partes = [p.strip() for p in resposta.replace(";", ",").split(",")]
    mapeamento = {}

    logger.info(f"Parse de categorização: {len(partes)} partes detectadas: {partes}")

    for parte in partes:
        tokens = parte.strip().split(" ", 1)
        if len(tokens) == 2:
            try:
                idx = int(tokens[0]) - 1  # converte para índice 0-based
                categoria_input = tokens[1].strip()
                resolved = _resolver_categoria(categoria_input)
                logger.info(f"  Item {idx+1}: input='{categoria_input}' → resolved='{resolved}'")
                if resolved and 0 <= idx < len(transactions):
                    mapeamento[idx] = resolved
                elif not resolved:
                    logger.warning(f"  Subcategoria '{categoria_input}' não reconhecida — mantendo Outros.")
                elif idx >= len(transactions):
                    logger.warning(f"  Índice {idx+1} fora do range ({len(transactions)} transações).")
            except ValueError:
                logger.warning(f"  Parte '{parte}' ignorada — número inválido.")
                continue

    logger.info(f"Mapeamento final: {mapeamento}")

    for idx, (categoria, subcategoria) in mapeamento.items():
        transactions[idx]["category"] = categoria
        transactions[idx]["subcategory"] = subcategoria

    return transactions


async def processar_ingestion_unificada(user_phone: str, all_transactions: list) -> tuple[str, list[dict] | None]:
    """
    Recebe a lista de transações extraídas.
    Retorna (mensagem, transacoes_outros_pendentes).
    - Se houver transações 'Outros', retorna a mensagem de categorização e a lista pendente.
    - Se não houver, grava tudo e retorna o diagnóstico final com None.
    """
    try:
        total_encontrado = len(all_transactions)
        logger.info(f"Recebidas {total_encontrado} transações para processamento.")

        if not all_transactions:
            return "✅ Processamento concluído: nenhuma transação foi encontrada no extrato.", None

        # 1. Filtra duplicatas por hash (primeira linha de defesa)
        all_tx_ids = list(set(str(tx.id).strip().lower() for tx in all_transactions if tx.id))
        ids_existentes = db.filtrar_transacoes_existentes(user_phone, all_tx_ids)
        logger.info(f"Deduplicação por hash: {len(ids_existentes)} IDs já conhecidos no banco.")

        novas_rows = []
        ids_no_lote = set()

        for tx in all_transactions:
            tx_id = str(tx.id).strip().lower()

            if tx_id in ids_existentes or tx_id in ids_no_lote:
                continue

            valor = abs(tx.amount)

            # Motor híbrido de categorização: regras do usuário → keywords globais → LLM
            # Passa (tx.category, tx.subcategory) como fallback caso o parser dedicado já tenha uma sugestão
            fallback = (tx.category, tx.subcategory) if tx.category and tx.category != "Outros" else None
            categoria_final, subcategoria_final = await categorizar_gasto_hibrido(
                user_phone, tx.description, fallback=fallback
            )

            # "Perguntar" é tratado como Outros — vai para a fila de confirmação manual
            if categoria_final == "Perguntar":
                categoria_final = "Outros"
                subcategoria_final = "Outros"

            novas_rows.append({
                "pluggy_transaction_id": tx_id,
                "user_phone": user_phone,
                "amount": valor,
                "category": categoria_final,
                "subcategory": subcategoria_final,
                "subcategory_id": db.get_subcategory_id_by_name(subcategoria_final) if subcategoria_final and subcategoria_final != "Outros" else None,
                "description": tx.description,
                "transaction_type": tx.type,
                "payment_method": tx.payment_method,
                "installment_of": tx.installment_of,
                "installment_total": tx.installment_total,
                "purchase_date": tx.date,
                "billing_date": getattr(tx, "billing_date", None) or tx.date,
            })
            ids_no_lote.add(tx_id)

        if not novas_rows:
            return (
                f"✅ Seu extrato já estava sincronizado!\n\n"
                f"🔍 Encontrei {total_encontrado} transações, mas todas já constavam no seu histórico."
            ), None

        # 2. Separa transações "Outros" para perguntar ao usuário
        outros = [r for r in novas_rows if r["category"] == "Outros"]
        nao_outros = [r for r in novas_rows if r["category"] != "Outros"]

        # 3. Grava imediatamente as transações já categorizadas
        # inserir_gastos_em_lote retorna int (inseridos reais) ou -1 (erro)
        inseridos_nao_outros = 0
        if nao_outros:
            inseridos_nao_outros = db.inserir_gastos_em_lote(nao_outros)
            if inseridos_nao_outros == -1:
                return "❌ Tive um problema ao salvar suas transações. Por favor, tente novamente.", None

        # 4. Se há transações "Outros", salva pendentes e pede ao usuário
        if outros:
            logger.info(f"Salvando {len(outros)} transações pendentes para {user_phone}")
            transactions_json = json.dumps(outros, ensure_ascii=False, default=str)
            logger.info(f"JSON size: {len(transactions_json)} chars")
            db.salvar_transacoes_pendentes(user_phone, transactions_json)
            # Verifica o que foi salvo
            salvo = db.obter_transacoes_pendentes(user_phone)
            if salvo:
                import json as _json
                salvo_list = _json.loads(salvo)
                logger.info(f"Verificação pós-save: {len(salvo_list)} transações no banco")
            else:
                logger.error("ERRO: pending_transactions não foi salvo no banco!")

            resumo = f"🔍 Encontrei {total_encontrado} transações no arquivo.\n"
            if nao_outros:
                duplicatas_banco = len(nao_outros) - inseridos_nao_outros
                resumo += f"✨ Importei *{inseridos_nao_outros} transações* com categoria identificada.\n"
                if duplicatas_banco > 0:
                    resumo += f"♻️ {duplicatas_banco} ignorados por já existirem.\n"
            if ids_existentes:
                resumo += f"♻️ {len(ids_existentes)} já constavam no seu histórico.\n"
            resumo += "\n"

            return resumo + _montar_mensagem_categorizacao(outros), outros

        # 5. Tudo categorizado — monta diagnóstico com números reais do banco
        duplicatas_banco = len(novas_rows) - inseridos_nao_outros
        total_ignorados = len(ids_existentes) + duplicatas_banco
        return _montar_diagnostico(total_encontrado, inseridos_nao_outros, total_ignorados), None

    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações.", None


async def gravar_transacoes_confirmadas(user_phone: str, transactions: list[dict]) -> str:
    """Grava no banco as transações após o usuário confirmar/corrigir as categorias."""
    try:
        if not transactions:
            return "✅ Nenhuma transação pendente para gravar."

        logger.info(f"Gravando {len(transactions)} transações confirmadas para {user_phone}")

        # Garante que o user_phone está correto em todas as rows (sobrescreve o valor do JSON salvo)
        # Isso evita dupla criptografia caso o JSON tenha sido salvo com valor já criptografado
        for tx in transactions:
            tx["user_phone"] = user_phone

        inseridos = db.inserir_gastos_em_lote(transactions)
        logger.info(f"inserir_gastos_em_lote retornou: {inseridos}")
        db.limpar_transacoes_pendentes(user_phone)

        if inseridos == -1:
            return "❌ Tive um problema ao salvar as transações confirmadas. Por favor, tente novamente."

        duplicatas = len(transactions) - inseridos
        msg = f"✅ *{inseridos} transações* salvas com sucesso! 🎉"
        if duplicatas > 0:
            msg += f"\n♻️ {duplicatas} ignoradas por já existirem."
        return msg

    except Exception as e:
        logger.error(f"Erro em gravar_transacoes_confirmadas: {e}", exc_info=True)
        return "❌ Erro técnico ao gravar transações confirmadas."


def _montar_diagnostico(total: int, novos: int, ignorados: int) -> str:
    return (
        f"🏦 *Extrato Processado!*\n\n"
        f"🔍 Encontrei {total} transações no arquivo.\n"
        f"✨ Importei *{novos} novos gastos*.\n"
        f"♻️ {ignorados} lançamentos ignorados por já existirem."
    )