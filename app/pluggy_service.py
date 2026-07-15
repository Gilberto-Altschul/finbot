import requests
import hashlib
import json   
import base64  
import logging
import time
from datetime import date
import app.database as db
from app.config import get_settings
from app.categorizer import categorizar_gasto_hibrido

logger = logging.getLogger(__name__)
settings = get_settings()

class PluggyService:
    def __init__(self):
        # 🔹 Autenticação via clientId/clientSecret

        logger.info(f"[DEBUG] clientId len={len(settings.pluggy_client_id)} sha256={hashlib.sha256(settings.pluggy_client_id.encode()).hexdigest()}")
        logger.info(f"[DEBUG] clientSecret len={len(settings.pluggy_client_secret)} sha256={hashlib.sha256(settings.pluggy_client_secret.encode()).hexdigest()}")

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

# --- INÍCIO DO LOG DE DEBUG PARA JWT ---
        if api_key and '.' in api_key:
            try:
                parts = api_key.split('.')
                if len(parts) >= 2:
                    payload = parts[1]
                    # Adiciona padding para base64 válido
                    payload += '=' * (-len(payload) % 4)
                    decoded_payload = json.loads(base64.urlsafe_b64decode(payload))
                    
                    logger.info("--- DEBUG JWT PLUGGY ---")
                    logger.info(f"Payload decodificado: {json.dumps(decoded_payload, indent=2)}")
                    logger.info("------------------------")
                    logger.info(f"Pluggy api_key completo: {api_key}")

            except Exception as e:
                logger.error(f"Erro ao decodificar JWT para debug: {e}")
        # --- FIM DO LOG DE DEBUG ---

        self.base_url = "https://api.pluggy.ai"
        self.headers = {
            "X-API-KEY": api_key,
            "accept": "application/json"
        }

    async def listar_itens(self):
        resp = requests.get(
            f"{self.base_url}/items",
            headers=self.headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def listar_contas(self, item_id: str):
        resp = requests.get(
            f"{self.base_url}/accounts",
            headers=self.headers,
            params={"itemId": item_id},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def verificar_status_sincronizacao(self, item_id: str):
        """Consulta o Pluggy para saber se o item terminou de processar."""
        logger.info(f"[DEBUG] item_id recebido: {item_id!r}")
        response = requests.get(
            f"{self.base_url}/items/{item_id}",
            headers=self.headers,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"Status do item {item_id}: {data.get('status')}")
        return data.get("status")

    async def sync_user_transactions(self, user_phone: str, account_id: str, item_id: str):
        # 1. Verifica status do item (não da conta)
        status = await self.verificar_status_sincronizacao(item_id)

        # Se não estiver UPDATED, retornamos um aviso ao usuário
        if status != "UPDATED":
            return f"A sincronização ainda está em andamento (Status: {status}). Aguarde um pouco e tente novamente.", None

        # 2. SE ESTIVER UPDATED: Busca as transações
        params = {
            "accountId": account_id,
            "dateFrom": "2026-07-01",
            "dateTo": date.today().isoformat()
        }
        
        tx_resp = requests.get(
            f"{self.base_url}/v2/transactions",
            headers=self.headers,
            params=params,
            timeout=30
        )
        logger.info(f"Pluggy request URL: {tx_resp.url}")
        logger.info(f"Pluggy response status: {tx_resp.status_code}")
        logger.info(f"Pluggy response body: {tx_resp.text[:2000]}")
        tx_resp.raise_for_status()
       
        transactions = tx_resp.json().get("results", [])
        
        # 3. Processa e insere no banco
        return await self._process_transactions(user_phone, transactions)
    
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

            descricao = tx.get("description") or tx.get("title")
            if not tx.get("amount") or not descricao:
                logger.warning(f"Transação descartada por falta de campos obrigatórios: {tx}")
                continue

            raw_amount = float(tx["amount"])
            tipo = "income" if raw_amount > 0 else "expense"

            # Categoria da Pluggy vem em inglês (ex: "Eating out") — mapeamos
            # para a taxonomia em português do FinBot usando o mesmo classificador
            # já usado no fluxo manual de categorização.
            categoria_pluggy = tx.get("category")
            try:
                categoria_pt, _ = await categorizar_gasto_hibrido(user_phone, descricao)
                if not categoria_pt or categoria_pt == "Perguntar":
                    categoria_pt = "Outros"
            except Exception as e:
                logger.warning(f"Falha ao categorizar '{descricao}' (categoria Pluggy: {categoria_pluggy}): {e}")
                categoria_pt = "Outros"

            row = {
                "user_phone": user_phone,
                "amount": abs(raw_amount),
                "category": categoria_pt,
                "description": descricao,
                "pluggy_transaction_id": tx.get("id"),
                "transaction_type": tipo,
                "payment_method": (tx.get("paymentMethod") or "debito").lower(),
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