# app/ingestion.py
import json
import logging
from datetime import date
from pydantic import ValidationError
from app.ofx_schema import OpenFinancePayload
import app.database as db

logger = logging.getLogger(__name__)

async def processar_ingestion_unificada(user_phone: str, json_padrao_str: str) -> str:
    """
    Lê o JSON gerado pelo tradutor de PDF e faz o upsert no banco de dados,
    disparando alertas baseados nos limites (coluna amount) de finbot_budgets.
    """
    try:
        # Validação rigorosa do payload via Pydantic
        data = OpenFinancePayload.model_validate_json(json_padrao_str)
        
        all_transactions = data.transactions
        logger.info(f"Recebidas {len(all_transactions)} transações do LLM para processamento.")

        if not all_transactions:
            logger.info(f"Nenhuma transação encontrada no payload para {user_phone}")
            return "✅ Processamento concluído: nenhuma transação foi encontrada no extrato."
        
        # 1. Filtra duplicatas em massa
        # Normalizamos os IDs para minúsculas antes de enviar para o banco
        all_tx_ids = list(set(str(tx.id).strip().lower() for tx in all_transactions if tx.id))
        # Buscamos no banco e garantimos que o set de comparação também esteja normalizado
        ids_existentes = {str(i).strip().lower() for i in db.filtrar_transacoes_existentes(user_phone, all_tx_ids)}
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
                "created_at": tx.date
            })
            
            ids_no_lote.add(tx_id)

        # 3. Faz o insert de tudo uma única vez
        if novas_rows:
            logger.info(f"Inserindo {len(novas_rows)} novas transações para {user_phone}")
            sucesso = db.inserir_gastos_em_lote(novas_rows)
            if not sucesso:
                return "❌ Tive um problema ao salvar suas transações no banco de dados. Por favor, tente novamente."

        novos_gastos = len(novas_rows)
        
        if novos_gastos == 0:
            return "✅ Seu extrato já estava totalmente sincronizado! Nenhuma nova transação foi importada."
            
        resposta = f"🏦 *Extrato Processado!*\n\nImportei *{novos_gastos} novos gastos* para o seu histórico."
        return resposta

    except ValidationError as ve:
        logger.error(f"Erro de validação no schema do Gemini: {ve.json()}")
        return "❌ O formato dos dados extraídos é inválido. Por favor, tente novamente."
    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações."