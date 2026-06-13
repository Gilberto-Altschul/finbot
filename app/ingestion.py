# app/ingestion.py
import logging
import json
from datetime import date
import app.database as db

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
    "lanche": ("Alimentação", "Lanche"),
    "padaria": ("Alimentação", "Padaria"),
    "delivery": ("Alimentação", "Delivery"),
    "ifood": ("Alimentação", "Delivery"),
    "cafe": ("Alimentação", "Café"),
    "café": ("Alimentação", "Café"),
    "comida": ("Alimentação", "Restaurante"),
    "hortifruti": ("Alimentação", "Hortifruti"),
    "acougue": ("Alimentação", "Açougue"),
    "açougue": ("Alimentação", "Açougue"),
    # Transporte
    "uber": ("Transporte", "Aplicativo"),
    "99": ("Transporte", "Aplicativo"),
    "taxi": ("Transporte", "Táxi"),
    "táxi": ("Transporte", "Táxi"),
    "onibus": ("Transporte", "Ônibus"),
    "ônibus": ("Transporte", "Ônibus"),
    "metro": ("Transporte", "Metrô"),
    "metrô": ("Transporte", "Metrô"),
    "gasolina": ("Transporte", "Combustível"),
    "combustivel": ("Transporte", "Combustível"),
    "combustível": ("Transporte", "Combustível"),
    "estacionamento": ("Transporte", "Estacionamento"),
    "pedagio": ("Transporte", "Pedágio"),
    "pedágio": ("Transporte", "Pedágio"),
    "manutencao": ("Transporte", "Manutenção"),
    "manutenção": ("Transporte", "Manutenção"),
    "seguro auto": ("Transporte", "Seguro Auto"),
    # Lazer
    "cinema": ("Lazer", "Cinema"),
    "streaming": ("Lazer", "Streaming"),
    "netflix": ("Lazer", "Streaming"),
    "spotify": ("Lazer", "Streaming"),
    "jogo": ("Lazer", "Games"),
    "games": ("Lazer", "Games"),
    "viagem": ("Lazer", "Viagem"),
    "hotel": ("Lazer", "Viagem"),
    "bar": ("Lazer", "Bar"),
    "balada": ("Lazer", "Balada"),
    "show": ("Lazer", "Show"),
    "teatro": ("Lazer", "Teatro"),
    "parque": ("Lazer", "Parque"),
    "diversao": ("Lazer", "Lazer Geral"),
    "diversão": ("Lazer", "Lazer Geral"),
    "entretenimento": ("Lazer", "Lazer Geral"),
    # Moradia
    "aluguel": ("Moradia", "Aluguel"),
    "condominio": ("Moradia", "Condomínio"),
    "condomínio": ("Moradia", "Condomínio"),
    "luz": ("Moradia", "Energia Elétrica"),
    "energia": ("Moradia", "Energia Elétrica"),
    "agua": ("Moradia", "Água"),
    "água": ("Moradia", "Água"),
    "gas": ("Moradia", "Gás"),
    "gás": ("Moradia", "Gás"),
    "internet": ("Moradia", "Internet"),
    "telefone": ("Moradia", "Telefone"),
    "celular": ("Moradia", "Celular"),
    "tv": ("Moradia", "TV por Assinatura"),
    "reforma": ("Moradia", "Reforma"),
    "mobilia": ("Moradia", "Mobília"),
    "mobília": ("Moradia", "Mobília"),
    "casa": ("Moradia", "Casa Geral"),
    # Saúde
    "farmacia": ("Saúde", "Farmácia"),
    "farmácia": ("Saúde", "Farmácia"),
    "remedio": ("Saúde", "Farmácia"),
    "remédio": ("Saúde", "Farmácia"),
    "medico": ("Saúde", "Consulta Médica"),
    "médico": ("Saúde", "Consulta Médica"),
    "consulta": ("Saúde", "Consulta Médica"),
    "dentista": ("Saúde", "Dentista"),
    "plano": ("Saúde", "Plano de Saúde"),
    "convenio": ("Saúde", "Plano de Saúde"),
    "convênio": ("Saúde", "Plano de Saúde"),
    "academia": ("Saúde", "Academia"),
    "exame": ("Saúde", "Exame"),
    "hospital": ("Saúde", "Hospital"),
    # Vestuário e Beleza
    "roupa": ("Vestuário e Beleza", "Roupa"),
    "roupas": ("Vestuário e Beleza", "Roupa"),
    "calcado": ("Vestuário e Beleza", "Calçado"),
    "calçado": ("Vestuário e Beleza", "Calçado"),
    "tenis": ("Vestuário e Beleza", "Calçado"),
    "tênis": ("Vestuário e Beleza", "Calçado"),
    "bolsa": ("Vestuário e Beleza", "Acessório"),
    "acessorio": ("Vestuário e Beleza", "Acessório"),
    "acessório": ("Vestuário e Beleza", "Acessório"),
    "salao": ("Vestuário e Beleza", "Salão"),
    "salão": ("Vestuário e Beleza", "Salão"),
    "cabelo": ("Vestuário e Beleza", "Salão"),
    "cosmetico": ("Vestuário e Beleza", "Cosméticos"),
    "cosmético": ("Vestuário e Beleza", "Cosméticos"),
    "perfume": ("Vestuário e Beleza", "Cosméticos"),
    "maquiagem": ("Vestuário e Beleza", "Cosméticos"),
    "moda": ("Vestuário e Beleza", "Roupa"),
    "vestuario": ("Vestuário e Beleza", "Roupa"),
    "vestuário": ("Vestuário e Beleza", "Roupa"),
    # Educação
    "escola": ("Educação", "Escola"),
    "colegio": ("Educação", "Escola"),
    "colégio": ("Educação", "Escola"),
    "faculdade": ("Educação", "Faculdade"),
    "universidade": ("Educação", "Faculdade"),
    "curso": ("Educação", "Curso"),
    "livro": ("Educação", "Livro"),
    "livros": ("Educação", "Livro"),
    "material": ("Educação", "Material Escolar"),
    "ingles": ("Educação", "Idioma"),
    "inglês": ("Educação", "Idioma"),
    # Pets
    "pet": ("Pets", "Pet Geral"),
    "animal": ("Pets", "Pet Geral"),
    "veterinario": ("Pets", "Veterinário"),
    "veterinário": ("Pets", "Veterinário"),
    "racao": ("Pets", "Ração"),
    "ração": ("Pets", "Ração"),
    "petshop": ("Pets", "Pet Shop"),
    # Financeiro
    "seguro": ("Financeiro", "Seguro"),
    "emprestimo": ("Financeiro", "Empréstimo"),
    "empréstimo": ("Financeiro", "Empréstimo"),
    "financiamento": ("Financeiro", "Financiamento"),
    "investimento": ("Financeiro", "Investimento"),
    "tarifa": ("Financeiro", "Tarifa Bancária"),
    "banco": ("Financeiro", "Tarifa Bancária"),
    "juros": ("Financeiro", "Juros"),
    # Extra
    "presente": ("Extra", "Presente"),
    "gift": ("Extra", "Presente"),
    "doacao": ("Extra", "Doação"),
    "doação": ("Extra", "Doação"),
    # Outros
    "outros": ("Outros", "Geral"),
    "geral": ("Outros", "Geral"),
}


def _resolver_categoria(subcategoria_input: str) -> tuple[str, str] | None:
    """
    Resolve (categoria, subcategoria) a partir de uma palavra do usuário.
    Tenta match exato no mapa de subcategorias.
    Retorna None se não reconhecido.
    """
    inp = subcategoria_input.strip().lower()
    return SUBCATEGORIA_MAP.get(inp, None)


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

            # Checa mapeamento do usuário para este estabelecimento
            mapping = db.get_user_merchant_mapping(user_phone, tx.description)
            if mapping:
                categoria_final = mapping["category_name"]
                subcategoria_final = mapping["subcategory_name"]
            else:
                categoria_final = tx.category
                subcategoria_final = tx.subcategory or "Geral"

            novas_rows.append({
                "pluggy_transaction_id": tx_id,
                "user_phone": user_phone,
                "amount": valor,
                "category": categoria_final,
                "subcategory": subcategoria_final,
                "description": tx.description,
                "transaction_type": tx.type,
                "payment_method": tx.payment_method,
                "installment_of": tx.installment_of,
                "installment_total": tx.installment_total,
                "purchase_date": tx.date,
                "billing_date": tx.date,
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
            transactions_json = json.dumps(outros, ensure_ascii=False, default=str)
            db.salvar_transacoes_pendentes(user_phone, transactions_json)

            resumo = ""
            if nao_outros:
                duplicatas_banco = len(nao_outros) - inseridos_nao_outros
                resumo = f"✨ Importei *{inseridos_nao_outros} transações* com categoria identificada.\n"
                if duplicatas_banco > 0:
                    resumo += f"♻️ {duplicatas_banco} ignorados por já existirem.\n"
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
