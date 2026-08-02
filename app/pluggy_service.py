import asyncio
import requests
import json   
import logging
import time
from datetime import date, timedelta
import app.database as db
from app.config import get_settings
from app.categorizer import categorizar_gasto_hibrido
from app.billing import _add_months

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
    async def obter_dia_corte_real(self, item_id: str, account_id: str) -> int | None:
        """
        Busca o dia de fechamento REAL da fatura direto da Pluggy
        (creditData.balanceCloseDate), em vez de depender só do dia_corte
        configurado manualmente no FinBot — que pode estar desatualizado ou
        errado. Retorna None se a conta não for de crédito ou o campo não
        vier preenchido (banco não suporta esse dado).
        """
        try:
            contas = await self.listar_contas(item_id)
            for conta in contas:
                if conta.get("id") == account_id:
                    credit_data = conta.get("creditData") or {}
                    close_date_str = credit_data.get("balanceCloseDate")
                    if close_date_str:
                        return date.fromisoformat(close_date_str[:10]).day
            return None
        except Exception as e:
            logger.warning(f"Não foi possível obter balanceCloseDate real da Pluggy: {e}")
            return None

    async def listar_faturas(self, account_id: str) -> list[dict]:
        """Busca as faturas (Bills) de uma conta de cartão de crédito na Pluggy."""
        resp = requests.get(
            f"{self.base_url}/bills",
            headers=self.headers,
            params={"accountId": account_id},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    async def obter_ultima_fatura_fechada_id(self, account_id: str) -> str | None:
        """
        Identifica o billId da última fatura JÁ FECHADA (billClosingDate
        preenchido, ou dueDate mais recente que já passou). Retorna None se
        a instituição não suportar a entidade Bills (nem toda instituição
        Direct suporta — só Regulado é obrigatório).
        """
        try:
            faturas = await self.listar_faturas(account_id)
            if not faturas:
                return None
            fechadas = [f for f in faturas if f.get("billClosingDate")]
            candidatas = fechadas or faturas
            candidatas.sort(key=lambda f: f.get("billClosingDate") or f.get("dueDate") or "", reverse=True)
            return candidatas[0]["id"] if candidatas else None
        except Exception as e:
            logger.warning(f"Não foi possível obter Bills da Pluggy (instituição pode não suportar): {e}")
            return None

    @staticmethod
    def _fatura_ja_fechou(purchase_date: date, dia_corte: int) -> bool:
        """
        Calcula se a fatura que contém essa compra já fechou de verdade,
        usando o dia de corte configurado — em vez de confiar no campo
        `status` da Pluggy, que pode demorar dias para ser atualizado
        depois que o banco já fechou a fatura de fato.
        """
        if purchase_date.day <= dia_corte:
            corte = date(purchase_date.year, purchase_date.month, dia_corte)
        else:
            proximo_mes = _add_months(date(purchase_date.year, purchase_date.month, 1), 1)
            corte = date(proximo_mes.year, proximo_mes.month, dia_corte)
        return date.today() > corte

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
        # Janela de ~45 dias cobre um ciclo de fatura completo com folga.
        # Combinado com o filtro de status abaixo, isso traz só a última
        # fatura fechada (não parcelas futuras/fatura aberta).
        params = {
            "accountId": account_id,
            "dateFrom": (date.today() - timedelta(days=45)).isoformat(),
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
        return await self._process_transactions(user_phone, transactions, item_id=item_id, account_id=account_id)

    async def _process_transactions(
        self, user_phone: str, transactions: list[dict], item_id: str | None = None, account_id: str | None = None
    ) -> tuple[str, list[dict] | None]:
        """
        Converte transações da Pluggy para o formato padrão e insere em lote no banco,
        com logs detalhados e tratamento de campos opcionais.
        """
        if not transactions:
            logger.info("Nenhuma transação recebida da Pluggy.")
            return "✅ Sincronização concluída. Nenhuma nova transação encontrada.", None

        rows = []
        pendentes_ignoradas = 0
        # Busca a última fatura FECHADA via Bills API (critério exato, quando suportado)
        bill_id_fatura_fechada = await self.obter_ultima_fatura_fechada_id(account_id) if account_id else None
        if bill_id_fatura_fechada:
            logger.info(f"Última fatura fechada identificada via Bills API: billId={bill_id_fatura_fechada}")

        # Fallback: dia de corte real da Pluggy, ou configurado manualmente
        dia_corte_real = None
        if not bill_id_fatura_fechada and item_id and account_id:
            dia_corte_real = await self.obter_dia_corte_real(item_id, account_id)

        if dia_corte_real:
            dia_corte = dia_corte_real
            logger.info(f"Usando dia de corte REAL da Pluggy: {dia_corte}")
        else:
            dia_corte, _dia_vencimento = db.get_card_settings(user_phone)
            logger.info(f"Usando dia de corte configurado: {dia_corte}")

        # ── Fase 1: prepara as transações válidas (rápido, sem I/O) ──────────
        preparadas = []
        for tx in transactions:
            logger.info(f"Transação recebida da Pluggy: {tx}")

            payment_method_raw = (tx.get("paymentMethod") or "").upper()
            credit_card_metadata = tx.get("creditCardMetadata") or {}
            eh_credito = bool(credit_card_metadata) or payment_method_raw not in ("", "OTHER", "PIX", "BOLETO", "TED", "DOC")

            purchase_date_str = (tx.get("date") or tx.get("transactionDate", ""))[:10]
            if eh_credito and bill_id_fatura_fechada:
                # Critério exato: só é "fechada" se o billId bater com a última
                # fatura fechada identificada via Bills API.
                is_forecast = credit_card_metadata.get("billId") != bill_id_fatura_fechada
            elif eh_credito and purchase_date_str:
                try:
                    purchase_date_obj = date.fromisoformat(purchase_date_str)
                    # Não confia no status da Pluggy (pode estar atrasado em relação
                    # ao fechamento real do banco) — calcula pelo dia de corte.
                    is_forecast = not self._fatura_ja_fechou(purchase_date_obj, dia_corte)
                except ValueError:
                    is_forecast = tx.get("status") == "PENDING"
            else:
                # Débito/PIX/boleto não têm ciclo de fatura — mantém o status da Pluggy
                is_forecast = tx.get("status") == "PENDING"

            if is_forecast:
                pendentes_ignoradas += 1  # mantém a contagem no log, agora não bloqueia mais

            descricao = tx.get("description") or tx.get("title")
            if not tx.get("amount") or not descricao:
                logger.warning(f"Transação descartada por falta de campos obrigatórios: {tx}")
                continue

            # Pagamento da própria fatura não é gasto real nem receita —
            # é só o registro administrativo de quitação, frequentemente
            # duplicado (boleto + confirmação de recebimento pelo mesmo valor).
            desc_lower_check = descricao.lower()
            if any(p in desc_lower_check for p in ["pagamento de fatura", "pagamento recebido", "pagto fatura", "pag fatura"]):
                logger.info(f"Ignorando pagamento de fatura (não é gasto real): {descricao}")
                continue

            moeda = tx.get("currencyCode", "BRL")
            if moeda != "BRL" and tx.get("amountInAccountCurrency") is not None:
                raw_amount = float(tx["amountInAccountCurrency"])
                logger.info(f"Conversão de moeda: {tx['amount']} {moeda} -> {raw_amount} BRL ({descricao})")
            else:
                if moeda != "BRL":
                    logger.warning(f"Transação em {moeda} sem amountInAccountCurrency — gravando valor bruto sem conversão: {tx}")
                raw_amount = float(tx["amount"])
            tipo_pluggy = (tx.get("type") or "").upper()
            if tipo_pluggy == "DEBIT":
                tipo = "expense"
            elif tipo_pluggy == "CREDIT":
                tipo = "income"
            else:
                # Fallback só se a Pluggy não informar o tipo — aí sim usa o sinal
                logger.warning(f"Transação sem campo 'type' da Pluggy, inferindo por sinal: {descricao}")
                tipo = "income" if raw_amount > 0 else "expense"

            categoria_pluggy = tx.get("category")
            categoria_dica = CATEGORIA_PLUGGY_PARA_PT.get(categoria_pluggy)

            preparadas.append({
                "tx": tx, "descricao": descricao, "raw_amount": raw_amount,
                "tipo": tipo, "categoria_dica": categoria_dica, "is_forecast": is_forecast,
            })

        # ── Fase 2: categoriza em paralelo (limite de concorrência para não
        # estourar rate limit da Gemini/Supabase) ────────────────────────────
        semaforo = asyncio.Semaphore(8)

        async def _categorizar_uma(item: dict) -> tuple[str, str | None]:
            async with semaforo:
                try:
                    fallback = (item["categoria_dica"], "Outros") if item["categoria_dica"] else None
                    categoria_pt, subcategoria_pt = await categorizar_gasto_hibrido(
                        user_phone, item["descricao"], fallback=fallback, permitir_melhor_esforco=True
                    )
                    if not categoria_pt or categoria_pt == "Perguntar":
                        categoria_pt = item["categoria_dica"] or "Outros"
                        subcategoria_pt = None
                    return categoria_pt, subcategoria_pt
                except Exception as e:
                    logger.warning(f"Falha ao categorizar '{item['descricao']}' (categoria Pluggy: {item['tx'].get('category')}): {e}")
                    return item["categoria_dica"] or "Outros", None

        resultados = await asyncio.gather(*[_categorizar_uma(item) for item in preparadas])

        # ── Fase 3: monta as linhas para inserção ────────────────────────────
        for item, (categoria_pt, subcategoria_pt) in zip(preparadas, resultados):
            tx, descricao, raw_amount, tipo = item["tx"], item["descricao"], item["raw_amount"], item["tipo"]
            subcategory_id = db.get_subcategory_id_by_name(subcategoria_pt) if subcategoria_pt else None
            row = {
                "user_phone": user_phone,
                "amount": abs(raw_amount),
                "category": categoria_pt,
                "subcategory": subcategoria_pt,
                "description": descricao,
                "pluggy_transaction_id": tx.get("id"),
                "transaction_type": tipo,
                "payment_method": (tx.get("paymentMethod") or "debito").lower(),
                "purchase_date": (tx.get("date") or tx.get("transactionDate", ""))[:10],
                "billing_date": (tx.get("creditCardDate") or tx.get("date") or tx.get("transactionDate", ""))[:10],
                "is_forecast": item["is_forecast"],
            }
            if subcategory_id:
                row["subcategory_id"] = subcategory_id
            rows.append(row)

        inseridos = db.inserir_gastos_em_lote(rows)
        logger.info(f"Sincronização finalizada: {inseridos} novas transações inseridas.")

        if inseridos == 0:
            return "✅ Seu extrato está atualizado. Nenhuma transação nova detectada.", None

        resumo = f"📌 *Novas transações encontradas:* {inseridos} lançamentos registrados."
        if bill_id_fatura_fechada:
            resumo += " (última fatura fechada)"
        return resumo, rows