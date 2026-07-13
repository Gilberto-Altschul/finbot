import requests
import json
import logging
from datetime import datetime, timedelta
import app.database as db
from app.config import get_settings
import app.agent as agent  # Para usar a inferência de categoria

logger = logging.getLogger(__name__)

class PluggyService:
    def __init__(self):
        settings = get_settings()
        self.client_id = settings.pluggy_client_id
        # Remove espaços e aspas acidentais que podem vir do .env
        self.client_secret = settings.pluggy_client_secret.strip().replace('"', '').replace("'", "") if settings.pluggy_client_secret else ""
        self.base_url = "https://api.pluggy.ai"

        self._api_key = None
        self._api_key_expires_at = None

        if not self.client_secret:
            logger.error("PLUGGY_CLIENT_SECRET não encontrado nas configurações! Verifique seu arquivo .env")

    def _get_api_key(self) -> str:
        """
        Autentica em /auth com clientId + clientSecret e retorna um apiKey válido.
        Reaproveita o apiKey em cache enquanto não expirar (~2h de validade real).
        """
        now = datetime.now()
        if self._api_key and self._api_key_expires_at and now < self._api_key_expires_at:
            return self._api_key

        url = f"{self.base_url}/auth"
        payload = {"clientId": self.client_id, "clientSecret": self.client_secret}
        response = requests.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

        self._api_key = data["apiKey"]
        self._api_key_expires_at = now + timedelta(minutes=110)  # margem de segurança
        logger.info(f"Novo apiKey Pluggy obtido: {self._api_key[:4]}...{self._api_key[-4:]}")
        return self._api_key

    @property
    def headers(self):
        return {
            "accept": "application/json",
            "x-api-key": self._get_api_key(),
        }

    def create_connect_token(self):
        """
        Gera um token temporário para abrir o widget da Pluggy e conectar um novo banco.
        Esse endpoint aceita clientId/clientSecret direto no payload, sem precisar do apiKey.
        """
        url = f"{self.base_url}/connect_token"
        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
        }
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            return response.json().get("accessToken")
        except Exception as e:
            logger.error(f"Erro ao gerar Connect Token: {e}")
            return None

    def sync_user_transactions(self, user_phone: str, account_id: str | None = None):
        """
        Executa o fluxo completo: Busca Item -> Puxa Transações -> 
        Salva no Banco -> Analisa Comportamento.
        """
        settings = get_settings()
        # Pilar 2: ITEMS - Busca o ID da conexão do banco deste usuário
        # Aqui tentamos buscar na tabela que criamos, ou usamos o default do .env
        item_id = db.get_user_item_id(user_phone) or settings.default_item_id
        
        if not item_id:
            return "❌ Nenhuma conta bancária conectada para este número."

        logger.info(f"Sincronizando transações para {user_phone} (Item: {item_id})")
        
        # Pilar 3: TRANSACTIONS - Busca os dados na Pluggy
        if account_id:
            # Se passarmos accountId, filtramos apenas uma conta específica
            url = f"{self.base_url}/transactions?accountId={account_id}"
        else:
            # Por padrão, busca todas as contas do item
            url = f"{self.base_url}/transactions?itemId={item_id}"
        
        try:
            response = requests.get(url, headers=self.headers)

            # Fallback se o trial estiver expirado (Erro 403)
            if response.status_code == 403:
                error_detail = response.json() if response.ok is False and response.text else "Sem detalhes"
                logger.error(f"Erro 403 na Pluggy (API_KEY_INVALID). Detalhes: {error_detail}")

                # Fallback automático para o arquivo local para não travar o desenvolvimento
                logger.warning("Usando fallback: processando 'transacoes.json' local.")
                return self.sync_from_file(user_phone, "transacoes.json")

            response.raise_for_status()
            transactions = response.json().get("results", [])
            return self._process_transactions(user_phone, transactions)

        except Exception as e:
            logger.error(f"Erro ao sincronizar Pluggy: {e}")
            return f"Desculpe, tive um problema ao conectar com seu banco: {str(e)}"

    def sync_from_file(self, user_phone: str, file_path: str):
        """
        Analisa um arquivo JSON local que contém o dump de transações da Pluggy.
        Útil para testes de categorização e importação manual.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # A Pluggy retorna os dados em uma lista 'results' ou diretamente como lista
            transactions = data.get("results", data) if isinstance(data, dict) else data
            return self._process_transactions(user_phone, transactions)
        except Exception as e:
            logger.error(f"Erro ao analisar arquivo JSON: {e}")
            return f"❌ Erro ao ler o arquivo JSON: {str(e)}"

    def _process_transactions(self, user_phone: str, transactions: list):
        """Salva no banco de dados as novas transações encontradas."""
        novos_gastos = 0
        novas_receitas = 0
        ignorados_duplicados = 0

        for tx in transactions:
            raw_amount = float(tx.get("amount", 0))
            amount = abs(raw_amount)
            tipo = "income" if raw_amount > 0 else "expense"

            # Determina o payment_method com base no campo 'type' da Pluggy
            pluggy_tx_type = tx.get("type", "").lower()
            payment_method = "credito" if pluggy_tx_type == "credit" else "debito"

            data_iso = tx.get("date")

            # Se o JSON for de Contas (Accounts) em vez de Transações, amount será 0
            if raw_amount == 0 and "balance" in tx:
                logger.warning(f"Ignorando item de saldo da conta: {tx.get('name')}")
                continue

            # Tenta inferir a categoria usando a lógica do Bot para manter consistência
            desc = tx.get("description", "Gasto automático")
            categoria_finbot = agent._infer_category(desc)
            if categoria_finbot == "Outros":
                # Mapeamento simples de categorias Pluggy -> FinBot
                cat_map = {"Salary": "Salário", "Electricity": "Moradia", "Housing": "Moradia", "Telecommunications": "Moradia"}
                pluggy_cat = tx.get("category", "Outros")
                categoria_finbot = cat_map.get(pluggy_cat, pluggy_cat)

            is_new = db.registrar_gasto_pluggy(
                user_phone=user_phone,
                valor=amount,
                categoria=categoria_finbot,
                descricao=desc,
                pluggy_id=tx["id"],
                tipo=tipo,
                data_tx=data_iso,
                payment_method=payment_method
            )

            if is_new:
                if tipo == "expense":
                    novos_gastos += 1
                else:
                    novas_receitas += 1
            else:
                ignorados_duplicados += 1

        logger.info(f"Sincronização finalizada: {novos_gastos} gastos, {novas_receitas} receitas.")

        if novos_gastos == 0 and novas_receitas == 0:
            resumo = "✅ Seu extrato está atualizado. Nenhuma transação nova detectada."
            if ignorados_duplicados > 0:
                resumo += f" ({ignorados_duplicados} transações já estavam no banco)."
            return resumo

        resumo = "📌 *Novas transações encontradas:*\n"
        if novos_gastos > 0:
            resumo += f"• {novos_gastos} novos gastos registrados.\n"
        if novas_receitas > 0:
            resumo += f"• {novas_receitas} novas receitas registradas.\n"

        return resumo

    def _get_mock_data(self):
        """Dados de fallback para quando a API estiver bloqueada (403)"""
        return [{
            "id": "mock_123",
            "description": "IFOOD *LUNCH",
            "amount": -85.50,
            "category": "Alimentação",
            "date": datetime.now().isoformat()
        }]