# app/pdf_import.py
import logging
import asyncio
import random
import re
import hashlib
import io
import base64
import pdfplumber
from google import genai
from google.genai import types, errors

from app.utils import _normalize
from app.config import get_settings
from app.ofx_schema import OpenFinancePayload, StandardTransaction
from app.parsers import detectar_banco_e_tipo, santander_credito, c6_credito

logger = logging.getLogger(__name__)
settings = get_settings()

_client = genai.Client(api_key=settings.gemini_api_key)


def _generate_transaction_hash_id(transaction: StandardTransaction, user_phone: str) -> str:
    """Gera ID determinístico para a transação pelo conteúdo."""
    normalized_desc = _normalize(transaction.description)
    amt_str = "{:.2f}".format(abs(transaction.amount))
    unique_string = f"{user_phone}|{transaction.date}|{amt_str}|{normalized_desc}|{transaction.type}"
    return hashlib.sha256(unique_string.encode()).hexdigest()


def _dict_to_standard_transaction(d: dict) -> StandardTransaction:
    """Converte dict de parser determinístico para StandardTransaction."""
    return StandardTransaction(
        id="temp",
        date=d['date'],
        description=d['description'],
        amount=d['amount'],
        category="Outros",  # categorização vem depois, na esteira de ingestion
        subcategory="Geral",
        installment_of=d.get('installment_of'),
        installment_total=d.get('installment_total'),
        payment_method=d.get('payment_method', 'credito'),
        type=d.get('type', 'expense'),
        billing_date=d.get('billing_date'),
    )


def _extrair_texto_pdf(pdf_content: bytes) -> str:
    """Extrai texto do PDF de forma determinística usando pdfplumber."""
    texto_paginas = []
    with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
        for i, page in enumerate(pdf.pages):
            texto = page.extract_text(layout=True)
            if texto:
                texto_paginas.append(f"=== PÁGINA {i+1} ===\n{texto}")
    texto_completo = "\n\n".join(texto_paginas)
    logger.info(f"pdfplumber extraiu {len(texto_completo)} chars")
    return texto_completo


def _tentar_parser_dedicado(pdf_content: bytes) -> dict | None:
    """
    Tenta usar um parser determinístico dedicado (Santander, C6, etc).
    Retorna None se o banco não for suportado ou o parse falhar.
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
            texto_completo = "\n\n".join(
                page.extract_text(layout=True) or "" for page in pdf.pages
            )

            deteccao = detectar_banco_e_tipo(texto_completo)
            if not deteccao:
                logger.info("Nenhum parser dedicado encontrado para este banco. Usando fallback Gemini.")
                return None

            banco, tipo = deteccao
            logger.info(f"Banco detectado: {banco} ({tipo})")

            if banco == "santander" and tipo == "credito":
                return santander_credito.parse_from_pdfplumber(pdf)
            elif banco == "c6" and tipo == "credito":
                return c6_credito.parse(texto_completo)
            else:
                logger.info(f"Parser para {banco}/{tipo} ainda não implementado. Usando fallback Gemini.")
                return None
    except Exception as e:
        logger.warning(f"Erro ao tentar parser dedicado: {e}. Usando fallback Gemini.")
        return None


async def _extrair_via_gemini(pdf_content: bytes, texto_pdf: str | None) -> list[StandardTransaction]:
    """Fallback: usa Gemini para extrair transações quando não há parser dedicado."""
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."

    fatura_mes = fatura_ano = None
    if texto_pdf:
        m = re.search(r'Vencimento\D{0,15}?(\d{2})/(\d{2})/(\d{4})', texto_pdf, re.IGNORECASE)
        if m:
            fatura_mes, fatura_ano = int(m.group(2)), int(m.group(3))
    if not fatura_mes:
        from datetime import datetime
        now = datetime.now()
        fatura_mes, fatura_ano = now.month, now.year
        logger.warning(f"Gemini fallback: não detectou mês/ano da fatura. Usando atual: {fatura_mes:02d}/{fatura_ano}")

    if texto_pdf and len(texto_pdf.strip()) > 100:
        logger.info("Usando abordagem híbrida: texto pdfplumber → Gemini")
        user_instructions = f"""
Analise o texto de extrato bancário abaixo e converta-o em um objeto JSON com a chave 'transactions'.

INFORMAÇÃO IMPORTANTE: Esta fatura é do mês {fatura_mes:02d}/{fatura_ano}.

ATENÇÃO: O extrato pode ter duas colunas de transações lado a lado. Extraia TODAS de AMBAS as colunas.

Regras:
1. Ignore cabeçalhos, subtotais, saldos e avisos.
2. Ignore linhas de Estorno e "Pagamento de fatura"/"Pag Fatura Boleto".
3. Para cada transação:
   - id: temporário baseado em data e valor
   - date: ISO YYYY-MM-DD. Formato original é DD/MM, NUNCA MM/DD.
     A fatura é {fatura_mes:02d}/{fatura_ano}. Regras de ano:
     - mês compra > {fatura_mes} → ano = {fatura_ano - 1}
     - mês compra ≤ {fatura_mes} → ano = {fatura_ano}
     NUNCA use ano {fatura_ano + 1}.
   - description: nome limpo do estabelecimento
   - amount: float positivo
   - category: Alimentação, Transporte, Lazer, Moradia, Saúde, Vestuário e Beleza, Educação, Pets, Financeiro, Extra, Outros
   - subcategory: subcategoria lógica
   - installment_of/installment_total: parcela atual/total (null se não parcelado)
   - payment_method: credito ou debito
   - type: expense ou income
- Valores negativos → type income, amount positivo.
- Responda APENAS com o JSON, sem markdown.

=== TEXTO DO EXTRATO ===
{texto_pdf}
"""
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_instructions)])]
    else:
        logger.warning("pdfplumber não extraiu texto suficiente. Usando fallback com PDF binário.")
        user_instructions = """
Analise o extrato bancário anexo e converta-o em um JSON com a chave 'transactions'.
Extraia TODAS as transações de ambas as colunas, se houver.
Ignore cabeçalhos, totais, estornos e pagamentos de fatura.
Para cada transação: id, date (ISO, DD/MM nunca MM/DD), description, amount (positivo),
category, subcategory, installment_of, installment_total, payment_method (credito/debito), type (expense/income).
Responda APENAS com o JSON.
"""
        pdf_b64 = base64.b64encode(pdf_content).decode()
        contents = [types.Content(role="user", parts=[
            types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf_b64)),
            types.Part.from_text(text=user_instructions),
        ])]

    models_to_try = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash-lite"]
    llm_response_text = None

    for model_name in models_to_try:
        logger.info(f"Tentando extração com modelo: {model_name}")
        for attempt in range(3):
            try:
                response = await _client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=OpenFinancePayload,
                        temperature=0.0,
                        max_output_tokens=32768,
                    ),
                )
                llm_response_text = response.text.strip() if response.text else ""
                if llm_response_text:
                    finish_reason = response.candidates[0].finish_reason
                    if finish_reason == "MAX_TOKENS":
                        logger.warning(f"Resposta de {model_name} truncada. Tentando próximo modelo.")
                        llm_response_text = None
                        break
                    logger.info(f"Extração bem-sucedida com {model_name}")
                    break
            except Exception as exc:
                err_str = str(exc)
                is_retryable = any(c in err_str for c in ["429", "503", "Resource has been exhausted"])
                if attempt < 2 and is_retryable:
                    wait = (attempt + 1) * 15 + random.uniform(5, 15)
                    logger.warning(f"{model_name} ocupado. Retentativa {attempt+1} em {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Modelo {model_name} falhou: {exc}")
                break
        if llm_response_text:
            break

    if not llm_response_text:
        raise ValueError("LLM did not return any content after multiple retries.")

    json_clean = re.sub(r'```json\s?|\s?```', '', llm_response_text).strip()
    payload = OpenFinancePayload.model_validate_json(json_clean)
    return payload.transactions


async def converter_pdf_nativo_para_json(pdf_content: bytes, user_phone: str) -> list[StandardTransaction]:
    """
    Pipeline principal: tenta parser determinístico dedicado primeiro (100% confiável).
    Se o banco não for suportado, cai no fallback híbrido pdfplumber+Gemini.
    """
    final_transactions: list[StandardTransaction] = []

    # 1. Tenta parser dedicado (determinístico, sem IA)
    resultado_dedicado = _tentar_parser_dedicado(pdf_content)

    if resultado_dedicado and resultado_dedicado['transactions']:
        logger.info(f"✅ Parser dedicado ({resultado_dedicado['bank_detected']}) extraiu {len(resultado_dedicado['transactions'])} transações.")
        raw_transactions = [_dict_to_standard_transaction(d) for d in resultado_dedicado['transactions']]
    else:
        # 2. Fallback: pdfplumber + Gemini
        try:
            texto_pdf = _extrair_texto_pdf(pdf_content)
        except Exception as e:
            logger.error(f"Erro ao extrair texto: {e}")
            texto_pdf = None
        raw_transactions = await _extrair_via_gemini(pdf_content, texto_pdf)

    # 3. Pós-processamento comum (filtros, hash, normalização)
    for tx in raw_transactions:
        desc_lower = _normalize(tx.description)

        if any(p in desc_lower for p in [
            "pagamento de fatura", "pagto fatura", "pag fatura", "pagamento fatura",
        ]):
            logger.info(f"Ignorando pagamento de fatura: {tx.description}")
            continue

        if tx.amount < 0:
            tx.amount = abs(tx.amount)
            tx.type = "income"

        tx.id = _generate_transaction_hash_id(tx, user_phone)

        if tx.payment_method:
            normalized_method = tx.payment_method.lower()
            if "credito" in normalized_method or "credit" in normalized_method:
                tx.payment_method = "credito"
            elif "debito" in normalized_method or "debit" in normalized_method:
                tx.payment_method = "debito"
            else:
                tx.payment_method = "debito"  # boleto, pix, etc

        final_transactions.append(tx)

    logger.info(f"Parsed {len(final_transactions)} transactions for {user_phone}.")
    return final_transactions
