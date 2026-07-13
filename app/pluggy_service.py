# app/pluggy_service.py
import logging
import requests
from datetime import date

import app.database as db
from app.config import get_settings

logger = logging.getLogger(__name__)

class PluggyService:
    def __init__(self):
        # 🔹 Faz a chamada ao endpoint de autenticação
        auth_resp = requests.post(
            "https://api.pluggy.ai/auth",
            json={
                "clientId": settings.pluggy_client_id,
                "clientSecret": settings.pluggy_client_secret
            },
            timeout=30
        )

        auth_resp.raise_for_status()
        api_key = auth_resp.json()["apiKey"]

        # 🔹 Usa o apiKey retornado para todas as chamadas seguintes
        self.base_url = "https://api.pluggy.ai"
        self.headers = {"X-API-KEY": api_key,
           "accept": "application/json"           
        }

    async def sync_user_transactions(self, user_phone: str, account_id: str = None):
        """
        Busca transações da Pluggy e processa para salvar no banco.
        """
        try:
            hoje = date.today()
            inicio_mes = hoje.replace(day=1).isoformat()
    
            params = {
                "accountId": account_id,
                "fromDate": inicio_mes,   # 🔹 primeiro dia do mês corrente
                "toDate": hoje.isoformat()  # 🔹 até hoje
            } if account_id else {
                "fromDate": inicio_mes,
                "toDate": hoje.isoformat()
            }
    
            tx_resp = requests.get(
                f"{self.base_url}/v2/transactions",
                headers=self.headers,
                params=params,
                timeout=30
            )            tx_resp.raise_for_status()

            # 🔹 Loga o JSON bruto no Railway
            print("JSON bruto da Pluggy:", tx_resp.text)

            data = tx_resp.json()
            transactions = data.get("results", [])

            resumo, rows = await self._process_transactions(user_phone, transactions)
            return resumo, rows

        except Exception as e:
            logger.error(f"Erro na sincronização Pluggy: {e}", exc_info=True)
            return f"❌ Falha na sincronização: {e}", None

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
