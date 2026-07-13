def sync_user_transactions(self, user_phone: str, account_id: str | None = None):
    """
    Executa o fluxo completo: Busca Item -> Lista Contas -> Puxa Transações ->
    Salva no Banco -> Analisa Comportamento.
    """
    settings = get_settings()
    item_id = db.get_user_item_id(user_phone) or settings.default_item_id

    if not item_id:
        return "❌ Nenhuma conta bancária conectada para este número."

    logger.info(f"Sincronizando transações para {user_phone} (Item: {item_id})")

    try:
        # Se foi passado um accountId, busca direto
        if account_id:
            url = f"{self.base_url}/v2/transactions"
            params = {"accountId": account_id}
            response = requests.get(url, headers=self.headers, params=params)
        else:
            # Primeiro lista as contas do item
            accounts_resp = requests.get(
                f"{self.base_url}/accounts",
                headers=self.headers,
                params={"itemId": item_id},
                timeout=15,
            )
            accounts_resp.raise_for_status()
            accounts = accounts_resp.json().get("results", [])

            if not accounts:
                return "⚠️ Nenhuma conta encontrada para este item."

            all_transactions = []
            for acc in accounts:
                acc_id = acc["id"]
                logger.info(f"Buscando transações da conta {acc_id} ({acc.get('name')})")

                tx_resp = requests.get(
                    f"{self.base_url}/v2/transactions",
                    headers=self.headers,
                    params={"accountId": acc_id, "dateFrom": "2025-07-01"},
                    timeout=15, 
                )

                if tx_resp.status_code != 200:
                    logger.error(f"Erro {tx_resp.status_code} na conta {acc_id}: {tx_resp.text}")
                    continue

                results = tx_resp.json().get("results", [])
                all_transactions.extend(results)

            return self._process_transactions(user_phone, all_transactions)

        # Tratamento de erro 403 (trial expirado)
        if response.status_code == 403:
            error_detail = response.json() if response.text else "Sem detalhes"
            logger.error(f"Erro 403 na Pluggy (API_KEY_INVALID). Detalhes: {error_detail}")
            logger.warning("Usando fallback: processando 'transacoes.json' local.")
            return self.sync_from_file(user_phone, "transacoes.json")

        response.raise_for_status()
        transactions = response.json().get("results", [])
        return self._process_transactions(user_phone, transactions)

    except Exception as e:
        logger.error(f"Erro ao sincronizar Pluggy: {e}")
        return f"Desculpe, tive um problema ao conectar com seu banco: {str(e)}"
