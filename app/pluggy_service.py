# app/pluggy_service.py
import logging
import requests
from datetime import date, timedelta

import app.database as db
import app.ingestion as ingestion
from app.config import get_settings
from app.ofx_schema import StandardTransaction

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
        """Converte transações da Pluggy para o formato padrão e envia para ingestão."""
        if not transactions:
            return "✅ Sincronização concluída. Nenhuma nova transação encontrada.", None

        standard_txns = []
        for tx in transactions:
            # Ignora transações sem valor ou descrição
            if not tx.get('amount') or not tx.get('description'):
                continue

            standard_txns.append(StandardTransaction(
                id=tx['id'],
                date=tx['date'][:10], # Pega apenas YYYY-MM-DD
                description=tx['description'],
                amount=abs(float(tx['amount'])),
                category="Outros", # A categorização ocorrerá na esteira de ingestão
                subcategory="Geral",
                type='income' if float(tx['amount']) > 0 else 'expense',
                payment_method=tx.get('paymentMethod', 'debito').lower(),
                billing_date=tx.get('creditCardDate', tx['date'])[:10],
            ))

        return await ingestion.processar_ingestion_unificada(user_phone, standard_txns)

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
            date_from = (date.today() - timedelta(days=90)).strftime('%Y-%m-%d')

            tx_resp = requests.get(
                f"{self.base_url}/v2/transactions",
                headers=self.headers,
                params={"accountId": target_account_id, "dateFrom": date_from},
                timeout=30,
            )
            tx_resp.raise_for_status()
            transactions = tx_resp.json().get("results", [])

            logger.info(f"Total de transações retornadas: {len(transactions)}")
            return await self._process_transactions(user_phone, transactions)

        except requests.HTTPError as e:
            logger.error(f"Erro HTTP ao sincronizar com a Pluggy: {e.response.text}")
            return f"❌ Tive um problema de comunicação com seu banco (HTTP {e.response.status_code}). Tente de novo.", None
        except Exception as e:
            logger.error(f"Erro inesperado ao sincronizar Pluggy: {e}", exc_info=True)
            return f"❌ Desculpe, um erro técnico inesperado ocorreu ao sincronizar.", None
