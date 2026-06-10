# app/ingestion.py
import logging
from datetime import date
import app.database as db

logger = logging.getLogger(__name__)

async def processar_ingestion_unificada(user_phone: str, all_transactions: list) -> str:
    """
    Recebe a lista de transações extraídas e faz o upsert no banco de dados,
    disparando alertas baseados nos limites (coluna amount) de finbot_budgets.
    """
    try:
        total_encontrado = len(all_transactions)
        logger.info(f"Recebidas {total_encontrado} transações para processamento.")

        if not all_transactions:
            logger.info(f"Nenhuma transação encontrada no payload para {user_phone}")
            return "✅ Processamento concluído: nenhuma transação foi encontrada no extrato."
        
        # 1. Filtra duplicatas em massa
        # Normalizamos os IDs para minúsculas antes de enviar para o banco
        all_tx_ids = list(set(str(tx.id).strip().lower() for tx in all_transactions if tx.id))
        # Buscamos no banco e garantimos que o set de comparação também esteja normalizado
        ids_existentes = db.filtrar_transacoes_existentes(user_phone, all_tx_ids)
        logger.info(f"Deduplicação: {len(ids_existentes)} IDs já conhecidos no banco.")
        logger.info(f"Total de IDs únicos do PDF para verificar: {len(all_tx_ids)}")
        
        novas_rows = []
        ids_no_lote = set() # Controle de unicidade dentro do lote atual

        for tx in all_transactions:
            # Normaliza o ID da transação atual para a comparação
            tx_id = str(tx.id).strip().lower()
            
            # Pula se já existe no banco OU se já foi adicionado neste lote (evita erro 21000)
            if tx_id in ids_existentes or tx_id in ids_no_lote:
                logger.info(f"Transação ignorada (ID: {tx_id}) - Já existe no banco: {tx_id in ids_existentes}, Já no lote: {tx_id in ids_no_lote}")
                continue
                
            valor = abs(tx.amount)
            
            logger.info(f"Novo gasto detectado: {tx.description} (ID: {tx_id})")
            
            # 2. Inteligência Otimizada:
            # Primeiro, checa se o usuário já ensinou uma regra para este local no banco (Rápido/Grátis)
            mapping = db.get_user_merchant_mapping(user_phone, tx.description)
            
            if mapping:
                categoria_final = mapping["category_name"]
                subcategoria_final = mapping["subcategory_name"]
            else:
                # Se não há regra no banco, usa o que a IA já extraiu do PDF (Não chama a LLM de novo!)
                categoria_final = tx.category
                subcategoria_final = tx.subcategory or "Geral"

            # Prepara a row para o insert em lote
            novas_rows.append({
                "user_phone": user_phone,
                "amount": valor,
                "category": categoria_final,
                "subcategory": subcategoria_final,
                "description": tx.description,
                "transaction_type": tx.type,
                "payment_method": tx.payment_method,
                "pluggy_transaction_id": tx_id,
                "installment_of": tx.installment_of,
                "installment_total": tx.installment_total,
                "purchase_date": tx.date,
                "billing_date": tx.date
            })
            
            ids_no_lote.add(tx_id)

        # 3. Faz o insert de tudo uma única vez
        if novas_rows:
            logger.info(f"Inserindo {len(novas_rows)} novas transações para {user_phone}")
            sucesso = db.inserir_gastos_em_lote(novas_rows)
            if not sucesso:
                return "❌ Tive um problema ao salvar suas transações no banco de dados. Por favor, tente novamente."

        novos_gastos = len(novas_rows)
        existentes = len(ids_existentes)
        
        if novos_gastos == 0:
            return f"✅ Seu extrato já estava sincronizado!\n\n🔍 Encontrei {total_encontrado} transações, mas todas as {existentes} novidades já constavam no seu histórico."
            
        resposta = (
            f"🏦 *Extrato Processado!*\n\n"
            f"🔍 Encontrei {total_encontrado} transações no arquivo.\n"
            f"✨ Importei *{novos_gastos} novos gastos*.\n"
            f"♻️ {existentes} lançamentos foram ignorados por já existirem."
        )
        return resposta

    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações."