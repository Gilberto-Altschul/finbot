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

    async def _process_transactions(self, user_phone: str, transactions: list[dict]) -> tuple[str, list[dict] | None]:
        """Converte transações da Pluggy e insere em lote no banco."""
        if not transactions:
            return "✅ Sincronização concluída. Nenhuma nova transação encontrada.", None

        rows = []
        for tx in transactions:
            if not tx.get("amount") or not tx.get("description"):
                continue

            raw_amount = float(tx["amount"])
            tipo = "income" if raw_amount > 0 else "expense"

            row = {
                "user_phone": user_phone,
                "amount": abs(raw_amount),
                "category": "Outros",  # categorização virá depois
                "description": tx["description"],
                "pluggy_transaction_id": tx["id"],
                "transaction_type": tipo,
                "payment_method": tx.get("paymentMethod", "debito").lower(),
                "purchase_date": tx["date"][:10],
                "billing_date": tx.get("creditCardDate", tx["date"])[:10],
            }
            rows.append(row)

        inseridos = db.inserir_gastos_em_lote(rows)
        logger.info(f"Sincronização finalizada: {inseridos} novas transações inseridas.")

        if inseridos == 0:
            return "✅ Seu extrato está atualizado. Nenhuma transação nova detectada.", None

        resumo = f"📌 *Novas transações encontradas:* {inseridos} lançamentos registrados."
        return resumo, rows

    async def sync_user_transactions(self, user_phone: str, account_id: str | None = None) -> tuple[str, list[dict] | None]:
        """
        Executa o fluxo completo: Busca Item -> Lista Contas -> Puxa Transações ->
        Salva no Banco -> Analisa Comportamento.
        """
        item_id = db.get_user_item_id(user_phone)
        if not item_id:
            return "❌ Nenhuma conta bancária conectada. Conecte seu banco primeiro.", None

        logger.info(f"Sincronizando transações para {user_phone} (Item: {item_id})")

        try:
            accounts_resp = requests.get(
                f"{self.base_url}/accounts",
                headers=self.headers,
                params={"itemId": item_id},
                timeout=20,
            )
            accounts_resp.raise_for_status()
            accounts = accounts_resp.json().get("results", [])

            if not accounts:
                return "⚠️ Nenhuma conta encontrada para este item.", None

            # Se não foi passado um account_id, usa a primeira conta encontrada
            target_account_id = account_id or accounts[0]['id']

            # Primeiro dia do mês atual
            primeiro_dia_mes = date.today().replace(day=1).strftime('%Y-%m-%d')

            tx_resp = requests.get(
                f"{self.base_url}/v2/transactions",
                headers=self.headers,
                params={"accountId": target_account_id, "dateFrom": primeiro_dia_mes},
                timeout=30,
            )
            logger.info(f"JSON bruto da Pluggy: {tx_resp.text}")
            tx_resp.raise_for_status()
            transactions = tx_resp.json().get("results", [])
            logger.info(f"Transações do mês vigente: {len(transactions)}")

            return await self._process_transactions(user_phone, transactions)

        except requests.HTTPError as e:
            logger.error(f"Erro HTTP ao sincronizar com a Pluggy: {e.response.text}")
            return f"❌ Tive um problema de comunicação com seu banco (HTTP {e.response.status_code}). Tente de novo.", None
        except Exception as e:
            logger.error(f"Erro inesperado ao sincronizar Pluggy: {e}", exc_info=True)
            return f"❌ Desculpe, um erro técnico inesperado ocorreu ao sincronizar.", None
