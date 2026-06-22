# app/parsers/base.py
"""Interface comum para parsers determinísticos de extratos/faturas bancárias."""


def detectar_banco_e_tipo(texto: str) -> tuple[str, str] | None:
    """
    Identifica o banco e o tipo de documento (credito/debito) a partir do texto extraído.
    Retorna (banco, tipo) ou None se não reconhecido.
    """
    texto_lower = texto.lower()

    # Santander
    if "santander" in texto_lower:
        if "parcelamentos" in texto_lower or "detalhamento da fatura" in texto_lower:
            return ("santander", "credito")
        return ("santander", "debito")

    # C6 Bank
    if "c6 bank" in texto_lower or "c6bank" in texto_lower or "c6 carbon" in texto_lower or "banco c6" in texto_lower:
        if "transações" in texto_lower and "cartão" in texto_lower:
            return ("c6", "credito")
        if "fatura" in texto_lower:
            return ("c6", "credito")
        return ("c6", "debito")

    return None
