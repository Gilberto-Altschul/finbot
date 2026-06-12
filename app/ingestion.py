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


def _montar_mensagem_categorizacao(transacoes_outros: list[dict]) -> str:
    """Monta a mensagem interativa pedindo categorias para transações 'Outros'."""
    linhas = ["🤔 *Não consegui categorizar estas transações:*\n"]
    for i, tx in enumerate(transacoes_outros, 1):
        valor = f"R$ {float(tx['amount']):.2f}".replace(".", ",")
        linhas.append(f"*{i}.* {tx['description']} — {valor}")

    linhas.append("\n📋 *Categorias disponíveis:*")
    linhas.append("Alimentação · Transporte · Lazer · Moradia · Saúde")
    linhas.append("Vestuário e Beleza · Educação · Pets · Financeiro · Extra")

    linhas.append("\n✏️ Responda no formato:")
    linhas.append("*1 Alimentação, 2 Transporte, 3 Lazer*")
    linhas.append("\nOu digite *ok* para salvar tudo como *Outros*.")
    return "\n".join(linhas)


def aplicar_categorizacao_usuario(transactions_json: str, resposta: str) -> list[dict]:
    """
    Aplica as categorias informadas pelo usuário nas transações pendentes.
    Formato esperado: '1 Alimentação, 2 Transporte' ou 'ok'
    """
    transactions = json.loads(transactions_json)

    if resposta.strip().lower() == "ok":
        return transactions  # Mantém tudo como Outros

    # Parse da resposta: "1 Alimentação, 2 Saúde, 3 Lazer"
    partes = [p.strip() for p in resposta.replace(";", ",").split(",")]
    mapeamento = {}

    for parte in partes:
        tokens = parte.strip().split(" ", 1)
        if len(tokens) == 2:
            try:
                idx = int(tokens[0]) - 1  # converte para índice 0-based
                categoria = tokens[1].strip().title()
                # Valida categoria
                match = next((c for c in CATEGORIAS_VALIDAS if c.lower() == categoria.lower()), None)
                if match and 0 <= idx < len(transactions):
                    mapeamento[idx] = match
            except ValueError:
                continue

    for idx, categoria in mapeamento.items():
        transactions[idx]["category"] = categoria
        transactions[idx]["subcategory"] = "Geral"

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

        # 1. Filtra duplicatas em massa
        all_tx_ids = list(set(str(tx.id).strip().lower() for tx in all_transactions if tx.id))
        ids_existentes = db.filtrar_transacoes_existentes(user_phone, all_tx_ids)
        logger.info(f"Deduplicação: {len(ids_existentes)} IDs já conhecidos no banco.")

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
            existentes = len(ids_existentes)
            return (
                f"✅ Seu extrato já estava sincronizado!\n\n"
                f"🔍 Encontrei {total_encontrado} transações, mas todas as {existentes} "
                f"novidades já constavam no seu histórico."
            ), None

        # 2. Separa transações "Outros" para perguntar ao usuário
        outros = [r for r in novas_rows if r["category"] == "Outros"]
        nao_outros = [r for r in novas_rows if r["category"] != "Outros"]

        # Grava imediatamente as transações já categorizadas
        if nao_outros:
            sucesso = db.inserir_gastos_em_lote(nao_outros)
            if not sucesso:
                return "❌ Tive um problema ao salvar suas transações no banco de dados. Por favor, tente novamente.", None

        # Se há transações "Outros", salva pendentes e pede ao usuário
        if outros:
            transactions_json = json.dumps(outros, ensure_ascii=False, default=str)
            db.salvar_transacoes_pendentes(user_phone, transactions_json)

            resumo = ""
            if nao_outros:
                resumo = f"✨ Importei *{len(nao_outros)} transações* com categoria identificada.\n\n"

            return resumo + _montar_mensagem_categorizacao(outros), outros

        # Tudo categorizado, retorna diagnóstico final
        return _montar_diagnostico(total_encontrado, len(novas_rows), len(ids_existentes)), None

    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações.", None


async def gravar_transacoes_confirmadas(user_phone: str, transactions: list[dict]) -> str:
    """Grava no banco as transações após o usuário confirmar/corrigir as categorias."""
    try:
        if not transactions:
            return "✅ Nenhuma transação pendente para gravar."

        sucesso = db.inserir_gastos_em_lote(transactions)
        db.limpar_transacoes_pendentes(user_phone)

        if not sucesso:
            return "❌ Tive um problema ao salvar as transações confirmadas. Por favor, tente novamente."

        return f"✅ *{len(transactions)} transações* salvas com sucesso! 🎉"

    except Exception as e:
        logger.error(f"Erro em gravar_transacoes_confirmadas: {e}")
        return "❌ Erro técnico ao gravar transações confirmadas."


def _montar_diagnostico(total: int, novos: int, existentes: int) -> str:
    return (
        f"🏦 *Extrato Processado!*\n\n"
        f"🔍 Encontrei {total} transações no arquivo.\n"
        f"✨ Importei *{novos} novos gastos*.\n"
        f"♻️ {existentes} lançamentos ignorados por já existirem."
    )
