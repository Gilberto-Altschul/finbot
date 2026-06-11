# app/pdf_import.py
import base64
import logging
import asyncio
import random
import re
import hashlib
from google import genai
from google.genai import types, errors

from app.config import get_settings  # type: ignore
from app.ofx_schema import OpenFinancePayload, StandardTransaction  # type: ignore

logger = logging.getLogger(__name__)
settings = get_settings()

_client = genai.Client(api_key=settings.gemini_api_key, http_options={'api_version': 'v1beta'})


def _generate_transaction_hash_id(transaction: StandardTransaction, user_phone: str) -> str:
    """Gera ID determinístico para a transação."""
    cleaned_desc = re.sub(r'\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?', '', transaction.description)
    raw_desc = "".join(filter(str.isalnum, cleaned_desc.lower()))
    amt_str = "{:.2f}".format(abs(transaction.amount))
    unique_string = f"{user_phone}|{transaction.date}|{amt_str}|{raw_desc}|{transaction.type}"
    return hashlib.sha256(unique_string.encode()).hexdigest()


async def converter_pdf_nativo_para_json(pdf_content: bytes, user_phone: str) -> list[StandardTransaction]:
    """
    Envia o PDF como base64 inline para o Gemini — sem File API.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."

    user_instructions = """
Analise o extrato bancário anexo e converta-o em um objeto JSON com a chave 'transactions'.

Regras:
1. Ignore linhas de cabeçalho, subtotais, saldos do dia e avisos.
2. Ignore linhas de Estorno.
3. Para cada transação (compras, boletos, pix, transferências):
   - id: ID temporário baseado na data e valor (ex: itau_20260503_1950)
   - date: ISO YYYY-MM-DD
   - description: nome limpo do estabelecimento (ex: DROGARIA SAO PAULO)
   - amount: float positivo
   - category: uma de: Alimentação, Transporte, Lazer, Moradia, Saúde, Vestuário e Beleza, Educação, Pets, Financeiro, Extra, Outros
   - subcategory: subcategoria lógica (ex: Mercado, Farmácia, Streaming)
   - installment_of: número da parcela atual (null se não parcelado)
   - installment_total: total de parcelas (null se não parcelado)
   - payment_method: credito ou debito
   - type: expense ou income

Regras específicas:
- Planos de Saúde ou Convênios → category: Saúde, NUNCA Financeiro
- Seguro Automóvel → category: Transporte
- PIX recebido, transferências recebidas, INSS recebido → type: income
- Responda APENAS com o JSON, sem markdown.
"""

    pdf_b64 = base64.b64encode(pdf_content).decode()

    # Modelos atuais em ordem de preferência (custo vs capacidade)
    modelos = [
        "gemini-2.5-flash",       # Principal — melhor custo/benefício, 1M tokens contexto
        "gemini-2.5-flash-lite",  # Fallback 1 — mais leve
        "gemini-2.0-flash-lite",  # Fallback 2 — estável
    ]

    llm_response_text = None

    for model_name in modelos:
        logger.info(f"Tentando extração com modelo: {model_name}")
        for attempt in range(3):
            try:
                response = await _client.aio.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[
                                types.Part(
                                    inline_data=types.Blob(
                                        mime_type="application/pdf",
                                        data=pdf_b64,
                                    )
                                ),
                                types.Part.from_text(text=user_instructions),
                            ],
                        )
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.1,
                        response_mime_type="application/json",
                        response_schema=OpenFinancePayload,
                        max_output_tokens=16384,  # Aumentado para evitar truncamento em extratos longos
                    ),
                )
                llm_response_text = response.text.strip() if response.text else ""
                if llm_response_text:
                    finish_reason = response.candidates[0].finish_reason
                    if finish_reason == "MAX_TOKENS":
                        logger.warning(f"⚠️ Resposta de {model_name} truncada (MAX_TOKENS). Tentando próximo modelo.")
                        llm_response_text = None  # Força tentar o próximo modelo
                        break
                    logger.info(f"Extração bem-sucedida com {model_name}")
                    break
            except (errors.ServerError, errors.ClientError) as exc:
                err_str = str(exc)
                is_retryable = "503" in err_str or "429" in err_str
                if attempt < 2 and is_retryable:
                    wait = (2 ** attempt) * 20 + random.uniform(2, 10)
                    logger.warning(f"{model_name} ocupado. Retentativa {attempt+1} em {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Modelo {model_name} falhou: {exc}")
                break

        if llm_response_text:
            break

    if not llm_response_text:
        raise ValueError("LLM did not return any content after multiple retries.")

    # Parse Pydantic e geração de IDs determinísticos
    payload = OpenFinancePayload.model_validate_json(llm_response_text)
    for tx in payload.transactions:
        tx.id = _generate_transaction_hash_id(tx, user_phone)

        # Normaliza payment_method para 'credito' ou 'debito' para corresponder às restrições do banco de dados
        if tx.payment_method:
            normalized_method = tx.payment_method.lower()
            if "credito" in normalized_method or "credit" in normalized_method:
                tx.payment_method = "credito"
            elif "debito" in normalized_method or "debit" in normalized_method:
                tx.payment_method = "debito"

    return payload.transactions
