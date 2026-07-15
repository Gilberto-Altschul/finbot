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

# Mapa das categorias oficiais da Pluggy (docs.pluggy.ai/docs/transaction-categories)
# para a taxonomia em português do FinBot (SISTEMA_CATEGORIAS em app/utils.py).
# Cobre tanto os nomes de nível 1 (ex: "Housing") quanto os de nível 2/3
# (ex: "Eating out"), já que a Pluggy pode retornar qualquer nível como `category`.
# Categorias sem correspondência clara caem em "Outros" e acionam o classificador
# por texto (categorizar_gasto_hibrido) como fallback.
CATEGORIA_PLUGGY_PARA_PT = {
    # Income
    "Income": "Financeiro",
    "Salary": "Financeiro",
    "Retirement": "Financeiro",
    "Entrepreneurial activities": "Empresa",
    "Government aid": "Financeiro",
    "Non-recurring income": "Financeiro",
    # Loans and Financing
    "Loans and Financing": "Financeiro",
    "Late payment and overdraft costs": "Financeiro",
    "Interests charged": "Financeiro",
    "Loans": "Financeiro",
    "Financing": "Financeiro",
    "Real estate financing": "Moradia",
    "Vehicle Financing": "Transporte",
    "Student loan": "Educação",
    # Investments
    "Investments": "Financeiro",
    "Automatic investment": "Financeiro",
    "Fixed income": "Financeiro",
    "Mutual funds": "Financeiro",
    "Variable income": "Financeiro",
    "Margin": "Financeiro",
    "Proceeds interests and dividends": "Financeiro",
    "Pension": "Financeiro",
    # Same person transfer / Transfers
    "Same person transfer": "Financeiro",
    "Same person transfer - Cash": "Financeiro",
    "Same person transfer - PIX": "Financeiro",
    "Same person transfer - TED": "Financeiro",
    "Transfers": "Financeiro",
    "Transfer": "Financeiro",
    "Transfer - Bank slip (Boleto)": "Financeiro",
    "Transfer - Cash": "Financeiro",
    "Transfer - Check": "Financeiro",
    "Transfer - DOC": "Financeiro",
    "Transfer - Foreign exchange": "Financeiro",
    "Transfer - Internal": "Financeiro",
    "Transfer - PIX": "Financeiro",
    "Transfer - TED": "Financeiro",
    "Credit card payment": "Financeiro",
    "Third-party transfers": "Financeiro",
    "Bank slip": "Financeiro",
    "Debt card": "Financeiro",
    "DOC": "Financeiro",
    "PIX": "Financeiro",
    "TED": "Financeiro",
    # Legal obligations
    "Legal obligations": "Financeiro",
    "Blocked balances": "Financeiro",
    "Alimony": "Família e Dependentes",
    # Services
    "Services": "Outros",
    "Telecommunications": "Moradia",
    "Internet": "Moradia",
    "Mobile": "Moradia",
    "TV": "Lazer",
    "Education": "Educação",
    "Online Courses": "Educação",
    "University": "Educação",
    "School": "Educação",
    "Kindergarten": "Educação",
    "Wellness and fitness": "Saúde",
    "Gyms and fitness centers": "Saúde",
    "Sports practice": "Lazer",
    "Wellness": "Saúde",
    "Tickets": "Lazer",
    "Stadiums and arenas": "Lazer",
    "Landmarks and museums": "Lazer",
    "Cinema, theater and concerts": "Lazer",
    # Shopping
    "Shopping": "Outros",
    "Online shopping": "Outros",
    "Electronics": "Outros",
    "Pet supplies and vet": "Pets",
    "Clothing": "Vestuário e Beleza",
    "Kids and toys": "Família e Dependentes",
    "Bookstore": "Educação",
    "Sports goods": "Lazer",
    "Office Supplies": "Empresa",
    "Cashback": "Financeiro",
    # Digital services
    "Digital services": "Lazer",
    "Gaming": "Lazer",
    "Video streaming": "Lazer",
    "Music streaming": "Lazer",
    # Groceries / Food and drinks
    "Groceries": "Alimentação",
    "Food and drinks": "Alimentação",
    "Eating out": "Alimentação",
    "Food delivery": "Alimentação",
    # Travel
    "Travel": "Lazer",
    "Airport and airlines": "Lazer",
    "Accommodation": "Lazer",
    "Mileage programs": "Lazer",
    "Bus tickets": "Transporte",
    # Donations / Gambling
    "Donations": "Outros",
    "Gambling": "Lazer",
    "Lottery": "Lazer",
    "Online bet": "Lazer",
    # Taxes
    "Taxes": "Financeiro",
    "Income taxes": "Financeiro",
    "Taxes on investments": "Financeiro",
    "Tax on financial operations": "Financeiro",
    # Bank fees
    "Bank fees": "Financeiro",
    "Account fees": "Financeiro",
    "Wire transfer fees and ATM fees": "Financeiro",
    "Credit card fees": "Financeiro",
    # Housing
    "Housing": "Moradia",
    "Rent": "Moradia",
    "Houseware": "Moradia",
    "Urban land and building tax": "Moradia",
    "Utilities": "Moradia",
    "Water": "Moradia",
    "Electricity": "Moradia",
    "Gas": "Moradia",
    # Healthcare
    "Healthcare": "Saúde",
    "Dentist": "Saúde",
    "Pharmacy": "Saúde",
    "Optometry": "Saúde",
    "Hospital clinics and labs": "Saúde",
    # Transportation
    "Transportation": "Transporte",
    "Taxi and ride-hailing": "Transporte",
    "Public transportation": "Transporte",
    "Car rental": "Transporte",
    "Bicycle": "Transporte",
    "Automotive": "Transporte",
    "Gas stations": "Transporte",
    "Parking": "Transporte",
    "Tolls and in-vehicle payment": "Transporte",
    "Vehicle ownership taxes and fees": "Transporte",
    "Vehicle maintenance": "Transporte",
    "Traffic tickets": "Transporte",
    # Insurance
    "Insurance": "Financeiro",
    "Life insurance": "Financeiro",
    "Home Insurance": "Moradia",
    "Health insurance": "Saúde",
    "Vehicle insurance": "Transporte",
    # Leisure / Other
    "Leisure": "Lazer",
    "Other": "Outros",
}
class PluggyService:
    def __init__(self):
        self.base_url = "https://api.pluggy.ai"
        self._api_key = None  # Cache interno do token

    def _ensure_authenticated(self):
        """Renova o token apenas se necessário ou se ele for nulo."""
        if not self._api_key:
            logger.info("Token ausente ou expirado. Renovando autenticação Pluggy...")
            auth_resp = requests.post(
                f"{self.base_url}/auth",
                json={
                    "clientId": settings.pluggy_client_id,
                    "clientSecret": settings.pluggy_client_secret
                },
                timeout=30
            )
            auth_resp.raise_for_status()
            self._api_key = auth_resp.json()["apiKey"]
        return {"X-API-KEY": self._api_key, "Content-Type": "application/json"}

    async def listar_itens(self):
        """Lista os itens (conexões) com autenticação dinâmica."""
        try:
            # Tenta a requisição
            headers = self._ensure_authenticated()
            resp = requests.get(f"{self.base_url}/items", headers=headers)
            
            # Se der 401, o token na memória pode ter expirado; limpa e tenta de novo
            if resp.status_code == 401:
                logger.warning("Token expirado durante a requisição. Renovando...")
                self._api_key = None
                headers = self._ensure_authenticated()
                resp = requests.get(f"{self.base_url}/items", headers=headers)
            
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            logger.error(f"Erro ao listar itens: {e}")
            raise e
                
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

    async def sync_user_transactions(self, user_phone: str, account_id: str):
            # 1. Consulta o status real no Pluggy (O segredo está aqui)
            resp = requests.get(f"{self.base_url}/accounts/{account_id}", headers=self.headers)
            acc_data = resp.json()
            status = acc_data.get("syncStatus")
    
            # 2. SE NÃO ESTIVER UPDATED, NÃO FAZEMOS NADA (Para o bot não dizer "Nenhuma transação")
            if status != "UPDATED":
                return f"⚠️ A sincronização ainda está em andamento (Status: {status}). O banco ainda não liberou os dados. Tente novamente em 1 minuto.", None
    
            # 3. SE ESTIVER UPDATED, BUSCA OS DADOS
            params = {
                "accountId": account_id, 
                "dateFrom": "2026-07-01", 
                "dateTo": date.today().isoformat()
            }
            
            tx_resp = requests.get(f"{self.base_url}/v2/transactions", headers=self.headers, params=params)
            tx_resp.raise_for_status()
            
            transactions = tx_resp.json().get("results", [])
            
            # 4. Processa e insere no banco (seu código atual)
            return await self._process_transactions(user_phone, transactions), None
    
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

            # 1ª tentativa: mapa estático a partir da categoria oficial da Pluggy
            # (sem custo, sem chamada de LLM). Só cai no classificador por texto
            # quando a Pluggy não retornou categoria ou ela não está mapeada.
            categoria_pluggy = tx.get("category")
            categoria_pt = CATEGORIA_PLUGGY_PARA_PT.get(categoria_pluggy)

            if categoria_pt is None:
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
    
    async def listar_transacoes_prontas(self, user_phone: str, account_id: str):
        # 1. Consulta o status real no Pluggy
        resp = requests.get(f"{self.base_url}/accounts/{account_id}", headers=self.headers)
        acc_data = resp.json()
        status = acc_data.get("syncStatus")

        # 2. Se não estiver pronto, retorna uma mensagem de aviso para o Agente
        if status != "UPDATED":
            return None, f"⚠️ A sincronização ainda está em andamento (Status: {status}). Por favor, tente novamente em 1 minuto."

        # 3. Se estiver UPDATED, prossegue com a busca (seu código atual de busca)
        params = {"accountId": account_id, "dateFrom": "2026-07-01", "dateTo": date.today().isoformat()}
        tx_resp = requests.get(f"{self.base_url}/v2/transactions", headers=self.headers, params=params)
        
        data = tx_resp.json()
        transactions = data.get("results", [])
        
        return await self._process_transactions(user_phone, transactions), None