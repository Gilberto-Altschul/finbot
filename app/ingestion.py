# app/ingestion.py
import json
import logging
import app.database as db

logger = logging.getLogger(__name__)

def processar_ingestion_unificada(user_phone: str, json_padrao_str: str) -> str:
    """
    Lê o JSON gerado pelo tradutor de PDF e faz o upsert no banco de dados,
    disparando alertas baseados nos limites (coluna amount) de finbot_budgets.
    """
    try:
        payload = json.loads(json_padrao_str)
        transactions = payload.get("transactions", [])
        
        novos_gastos = 0
        alertas_comportamentais = []
        
        for tx in transactions:
            valor = abs(tx["amount"]) # Supabase armazena como positivo
            
            # Tenta cadastrar no banco. Se retornar True, o ID único funcionou e evitou duplicação
            foi_inserido = db.registrar_gasto_automatico(
                user_phone=user_phone,
                valor=valor,
                category=tx["category"],
                description=tx["description"],
                tx_id=tx["id"],
                metodo=tx["payment_method"],
                data_tx=tx.get("date")
            )
            
            if foi_inserido:
                novos_gastos += 1
                
                # Análise em tempo real do orçamento do usuário
                limite_meta = db.get_budget_limit(user_phone, tx["category"])
                if limite_meta:
                    total_gasto_mes = db.category_total(user_phone, tx["category"])
                    percentual = (total_gasto_mes / limite_meta) * 100
                    
                    if percentual >= 100:
                        alertas_comportamentais.append(f"🚨 *ESTOUROU A META!* O gasto em '{tx['description']}' fez você estourar o limite de {tx['category']}.")
                    elif percentual >= 80:
                        alertas_comportamentais.append(f"⚠️ *ALERTA:* Seus gastos com '{tx['category']}' atingiram {percentual:.0f}% da meta mensal.")
                        
        if novos_gastos == 0:
            return "✅ Seu extrato já estava totalmente sincronizado! Nenhuma nova transação foi importada."
            
        resposta = f"🏦 *Extrato C6 Processado!*\n\nImportei *{novos_gastos} novos gastos* para o seu histórico.\n"
        if alertas_comportamentais:
            resposta += "\n⚠️ *Análise do seu Coach Financeiro:*\n" + "\n".join(alertas_comportamentais)
        else:
            resposta += "\n👍 Boa! Todas as transações estão dentro do seu planejamento mensal."
            
        return resposta

    except Exception as e:
        logger.error(f"Erro na esteira de ingestão: {e}")
        return "❌ Erro ao processar o formato estruturado das transações."