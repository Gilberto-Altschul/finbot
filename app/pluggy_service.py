import requests
import logging
from datetime import date
import app.database as db
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class PluggyService:
    def __init__(self):
        # 🔹 Autenticação via clientId/clientSecret
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

        self.base_url = "https://api.pluggy.ai"
        self.headers = {
            "X-API-KEY": api_key,
            "accept": "application/json"
        }

    async def listar_itens(self):
        resp = requests.get(
            f"{self.base_url}/v2/items",
            headers=self.headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def listar_contas(self, item_id: str):
        resp = requests.get(
            f"{self.base_url}/v2/accounts",
            headers=self.headers,
            params={"itemId": item_id},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def sync_user_transactions(self, user_phone: str, account_id: str):
        hoje = date.today()
        inicio_mes = hoje.replace(day=1).isoformat()

        params = {
            "accountId": account_id,
            "dateFrom": inicio_mes,
            "dateTo": hoje.isoformat()
        }

        tx_resp = requests.get(
            f"{self.base_url}/v2/transactions",
            headers=self.headers,
            params=params,
            timeout=30
        )
        tx_resp.raise_for_status()

        data = tx_resp.json()
        transactions = data.get("results", [])
        resumo, rows = await self._process_transactions(user_phone, transactions)
        return resumo, rows
