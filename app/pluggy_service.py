# app/pluggy_service.py
import logging
import requests
from datetime import date

import app.database as db
from app.config import get_settings

logger = logging.getLogger(__name__)


class PluggyService:
    def __init__(self):
        settings = get_settings()
        self.base_url = "https://api.pluggy.ai"
        self.headers = {
            "X-API-KEY": settings.pluggy_api_key,
            "accept": "application/json"
        }


    async def _process_transactions(
        self, user_phone: str, transactions: list[dict]
    ) -> tuple[str, list[dict] | None]:
        """
        Converte transações da Pluggy para o formato padrão e insere em lote no banco,
        com logs detalhados e tratamento de campos opcionais.
        """
        if not transactions:
            logger.info("Nenhuma transação recebida da Pluggy.")
            return "✅ Sincronização concluída. Nenhuma nova transação encontrada.", None
    
        rows = []
        for tx in transactions:
            logger.info(f"Transação recebida da Pluggy: {tx}")  # loga cada transação bruta
    
            # Aceita tanto 'description' quanto 'title'
            descricao = tx.get("description") or tx.get("title")
            if not tx.get("amount") or not descricao:
                logger.warning(f"Transação descartada por falta de campos obrigatórios: {tx}")
                continue
    
            raw_amount = float(tx["amount"])
            tipo = "income" if raw_amount > 0 else "expense"
    
            row = {
                "user_phone": user_phone,
                "amount": abs(raw_amount),
                "category": tx.get("category", "Outros"),  # categorização virá depois
                "description": descricao,
                "pluggy_transaction_id": tx.get("id"),
                "transaction_type": tipo,
                "payment_method": (tx.get("paymentMethod") or "debito").lower(),
                # Aceita tanto 'date' quanto 'transactionDate'
                "purchase_date": (tx.get("date") or tx.get("transactionDate", ""))[:10],
                "billing_date": (tx.get("creditCardDate") or tx.get("date") or tx.get("transactionDate", ""))[:10],
            }
            rows.append(row)
    
        inseridos = db.inserir_gastos_em_lote(rows)
        logger.info(f"Sincronização finalizada: {inseridos} novas transações inseridas.")
    
        if inseridos == 0:
           return "✅ Seu extrato está atualizado. Nenhuma transação nova detectada.", None
    
        resumo = f"📌 *Novas transações encontradas:* {inseridos} lançamentos registrados."
        return resumo, rows
