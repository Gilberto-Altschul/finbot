# app/ingestion.py
import json
import logging
from datetime import date
from pydantic import ValidationError
from app.ofx_schema import OpenFinancePayload
import app.database as db

logger = logging.getLogger(__name__)

def processar_ingestion_unificada(user_phone: str, json_padrao_str: str) -> str:
    """
    Lê o JSON gerado pelo tradutor de PDF e faz o upsert no banco de dados,
    disparando alertas baseados nos limites (coluna amount) de finbot_budgets.
    """
    try:
        # Validação rigorosa do payload via Pydantic
        data = OpenFinancePayload.model_validate_json(json_padrao_str)
        
        all_transactions = data.transactions

        if not all_transactions:
            logger.info(f"Nenhuma transação encontrada no payload para {user_phone}")
            return "✅ Processamento concluído: nenhuma transação foi encontrada no extrato."
        
        mes_atual = date.today().strftime("%Y-%m")
        
        # OTIMIZAÇÃO: Busca todos os orçamentos e totais acumulados de uma vez só
        # Evita o problema N+1 de requisições ao banco de dados
        budgets = {b["category"].lower(): float(b["amount"]) for b in db.get_all_budgets(user_phone, mes_atual)}
        running_totals = {c["category"].lower(): float(c["total"]) for c in db.monthly_by_category(user_phone)}
        
        alertas_comportamentais = []
        categorias_alertadas = set()
        
        # 1. Filtra duplicatas em massa
        all_tx_ids = [tx.id for tx in all_transactions]
        ids_existentes = db.filtrar_transacoes_existentes(user_phone, all_tx_ids)
        
        novas_rows = []
        ids_no_lote = set() # Controle de unicidade dentro do lote atual

        for tx in all_transactions:
            tx_id = tx.id
            # Pula se já existe no banco OU se já foi adicionado neste lote (evita erro 21000)
            if tx_id in ids_existentes or tx_id in ids_no_lote:
                if tx_id in ids_no_lote:
                    logger.warning(f"ID duplicado detectado no mesmo PDF: {tx_id}. Ignorando.")
                continue
                
            valor = abs(tx.amount)
            # Prepara a row para o insert em lote
            novas_rows.append({
                "user_phone": user_phone,
                "amount": valor,
                "category": tx.category,
                "description": tx.description,
                "transaction_type": tx.type,
                "payment_method": tx.payment_method,
                "pluggy_transaction_id": tx_id,
                "created_at": tx.date
            })
            
            ids_no_lote.add(tx_id)

            # 2. Atualiza totais locais e gera alertas sem consultar o banco no loop
            cat_key = tx.category.lower()
            running_totals[cat_key] = running_totals.get(cat_key, 0.0) + valor
            
            limite_meta = budgets.get(cat_key)
            if limite_meta and cat_key not in categorias_alertadas:
                percentual = (running_totals[cat_key] / limite_meta) * 100
                if percentual >= 100:
                    alertas_comportamentais.append(f"🚨 *ESTOUROU A META!* O gasto em '{tx.description}' excedeu o limite de {tx.category}.")
                    categorias_alertadas.add(cat_key)
                elif percentual >= 80:
                    alertas_comportamentais.append(f"⚠️ *ALERTA:* Gastos com '{tx.category}' atingiram {percentual:.0f}% da meta.")
                    categorias_alertadas.add(cat_key)

        # 3. Faz o insert de tudo uma única vez
        if novas_rows:
            sucesso = db.inserir_gastos_em_lote(novas_rows)
            if not sucesso:
                return "❌ Tive um problema ao salvar suas transações no banco de dados. Por favor, tente novamente."

        novos_gastos = len(novas_rows)
        
        if novos_gastos == 0:
            return "✅ Seu extrato já estava totalmente sincronizado! Nenhuma nova transação foi importada."
            
        resposta = f"🏦 *Extrato C6 Processado!*\n\nImportei *{novos_gastos} novos gastos* para o seu histórico.\n"
        if alertas_comportamentais:
            resposta += "\n⚠️ *Análise do seu Coach Financeiro:*\n" + "\n".join(alertas_comportamentais)
        else:
            resposta += "\n👍 Boa! Todas as transações estão dentro do seu planejamento mensal."
            
        return resposta

    except ValidationError as ve:
        logger.error(f"Erro de validação no schema do Gemini: {ve.json()}")
        return "❌ O formato dos dados extraídos é inválido. Por favor, tente novamente."
    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações."