import requests
import json   
import base64  
import logging
import time
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

    async def verificar_status_sincronizacao(self, account_id: str):
        """Consulta o Pluggy para saber se a conta terminou de processar."""
        response = requests.get(
            f"{self.base_url}/accounts/{account_id}", 
            headers=self.headers
        )
        data = response.json()
        return data.get("syncStatus")

    async def sync_user_transactions(self, user_phone: str, account_id: str):
        # O método de autenticação (Auth) acontece no __init__ (primeira chamada)
        # Aqui fazemos a segunda chamada (Transactions)
        status = await self.verificar_status_sincronizacao(account_id)
        
        if status != "UPDATED":
            # Retorna uma mensagem de aviso em vez de tentar buscar dados vazios
            return f"A sincronização ainda está em andamento (Status: {status}). Aguarde um pouco e tente novamente.", []
        
        params = {
                    "accountId": "8eb1ed47-ccd8-4018-8da3-63f1369aeb86",
                    "dateFrom": "2026-07-01",
                    "dateTo": "2026-07-14"
                }
                
        tx_resp = requests.get(
                f"{self.base_url}/v2/transactions",
                headers=self.headers,
                params=params,
                timeout=30
        )                
        # LOGS DE DIAGNÓSTICO (O que o Python realmente recebeu?)
        print(f"DEBUG: Status Code: {tx_resp.status_code}")
        print(f"DEBUG: Texto da Resposta: {tx_resp.text[:500]}") # Imprime os primeiros 500 caracteres
                
        tx_resp.raise_for_status()

        
#        params = {
#            "accountId": account_id,
#           "dateFrom": "2026-07-01", # Ajuste conforme necessário
#           "dateTo": date.today().isoformat()
#      }
    
#       status = requests.get(f"{self.base_url}/accounts/{account_id}", headers=self.headers).json()
#        print(f"DEBUG: Status atual da conta: {status.get('syncStatus')}")    

#        print("DEBUG: Aguardando 15 segundos para sincronização do Pluggy...")
#        time.sleep(15)

#        tx_resp = requests.get(
#           f"{self.base_url}/v2/transactions",
#           headers=self.headers,
#           params=params,
#            timeout=30
#        )
        
        data = tx_resp.json()

#        # LOG COMPLETO PARA DIAGNÓSTICO 
#        print(f"DEBUG TOTAL: Status Code: {tx_resp.status_code}")
#        print(f"DEBUG TOTAL: Dados brutos recebidos: {data}")
#        print(f"DEBUG: URL chamada: {tx_resp.url}")


        if not data.get("results"):
            print(f"DEBUG: Resposta vazia. Payload completo: {data}")
        else:
            print(f"DEBUG: Encontradas {len(data['results'])} transações.")        

        transactions = data.get("results", []) 
        print(f"DEBUG: Quantidade de transações encontradas: {len(transactions)}")               
        resumo, rows = await self._process_transactions(user_phone, transactions)
        return resumo, rows

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

            row = {
                "user_phone": user_phone,
                "amount": abs(raw_amount),
                "category": tx.get("category", "Outros"),
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
