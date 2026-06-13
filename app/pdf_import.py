# app/pdf_import.py
import base64
import logging
import asyncio
import random
import re
import hashlib
from google import genai
from google.genai import types, errors

from app.utils import _normalize # Import _normalize
from app.config import get_settings  # type: ignore
from app.ofx_schema import OpenFinancePayload, StandardTransaction  # type: ignore

logger = logging.getLogger(__name__)
settings = get_settings()

# Deixamos o SDK gerenciar a versão da API automaticamente para evitar erros 404/400
_client = genai.Client(api_key=settings.gemini_api_key)


def _generate_transaction_hash_id(transaction: StandardTransaction, user_phone: str) -> str:
    """Gera ID determinístico para a transação pelo conteúdo, sem depender da posição."""
    normalized_desc = _normalize(transaction.description)
    amt_str = "{:.2f}".format(abs(transaction.amount))
    unique_string = f"{user_phone}|{transaction.date}|{amt_str}|{normalized_desc}|{transaction.type}"
    return hashlib.sha256(unique_string.encode()).hexdigest()


async def converter_pdf_nativo_para_json(pdf_content: bytes, user_phone: str) -> list[StandardTransaction]:
    """
    Envia o PDF como base64 inline para o Gemini — sem File API.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."

    user_instructions = """
Analise o extrato bancário anexo e converta-o em um objeto JSON com a chave 'transactions'.

ATENÇÃO: O extrato pode ter duas colunas de transações lado a lado (esquerda e direita).
Leia e extraia TODAS as transações de AMBAS as colunas. Não ignore nenhuma coluna ou seção.

Regras:
1. Ignore linhas de cabeçalho, subtotais, saldos do dia e avisos.
2. Ignore linhas de Estorno.
3. Para cada transação (compras, boletos, pix, transferências):
   - **IMPORTANTE**: Ignore transações de "Pagamento de fatura", "Pagto fatura" ou similares.
   - id: ID temporário baseado na data e valor (ex: itau_20260503_1950)
   - date: data ORIGINAL da compra no formato ISO YYYY-MM-DD.
     IMPORTANTE: use sempre a coluna "Compra" ou "Data" do extrato, NUNCA a data de lançamento na fatura.
     As datas no extrato aparecem no formato DD/MM (dia/mês) sem ano. NUNCA interprete como MM/DD.
     Exemplo: "03/05" significa dia 03 de maio, NÃO dia 05 de março.
     Use as seguintes regras para determinar o ano:
     - Se o mês da compra for MAIOR que o mês da fatura → ano da compra = ano da fatura - 1
     - Se o mês da compra for MENOR ou IGUAL ao mês da fatura → ano da compra = ano da fatura
     Exemplo com fatura de abril/2026 (mês 04, ano 2026):
       - compra em "31/08" → mês 08 > mês 04 → ano 2025 → date: 2025-08-31
       - compra em "25/02" → mês 02 < mês 04 → ano 2026 → date: 2026-02-25
       - compra em "03/05" → mês 05 > mês 04 → ano 2025 → date: 2025-05-03
       - compra em "01/04" → mês 04 = mês 04 → ano 2026 → date: 2026-04-01
     O mês e ano da fatura atual estão indicados no cabeçalho do PDF.
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
- **Transações com valor negativo no PDF original** devem ser tratadas como `type: income` e o `amount` deve ser retornado como valor positivo.
- Responda APENAS com o JSON, sem markdown.
"""

    pdf_b64 = base64.b64encode(pdf_content).decode()

    # Modelos atuais em ordem de preferência (custo vs capacidade)
    models_to_try = [
        "gemini-2.5-flash-lite",  # Principal — rápido, barato e suporta PDFs
        "gemini-2.5-flash",       # Fallback — mais capaz
        "gemini-2.0-flash-lite",  # Fallback estável
    ]

    llm_response_text = None

    for model_name in models_to_try:
        logger.info(f"Tentando extração com modelo: {model_name}")
        for attempt in range(3):
            try:
                response = await _client.aio.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Content(
                            role="user", # O role do conteúdo é sempre "user"
                            parts=[ # O conteúdo é uma lista de partes
                                types.Part( # A primeira parte é o PDF
                                    inline_data=types.Blob( # Dados inline para o PDF
                                        mime_type="application/pdf", # Tipo MIME do PDF
                                        data=pdf_b64, # Dados do PDF em base64
                                    ) # Fim da parte do PDF
                                ),
                                types.Part.from_text(text=user_instructions),
                            ],
                        )
                    ],
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
                        logger.warning(f"⚠️ Resposta de {model_name} truncada (MAX_TOKENS). Tentando próximo modelo.")
                        llm_response_text = None  # Força tentar o próximo modelo
                        logger.debug(f"Truncated LLM response (first 500 chars): {response.text[:500]}")
                        break
                    logger.info(f"Extração bem-sucedida com {model_name}")
                    break
            except Exception as exc:
                err_str = str(exc)
                # Detecta se é erro de cota ou sobrecarga (429 ou 503)
                is_retryable = any(code in err_str for code in ["429", "503", "Resource has been exhausted"])
                
                if attempt < 2 and is_retryable:
                    wait = (attempt + 1) * 15 + random.uniform(5, 15)
                    logger.warning(f"⚠️ {model_name} ocupado/cota excedida. Retentativa {attempt+1} em {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Modelo {model_name} falhou: {exc}")
                break

        if llm_response_text:
            break

    if not llm_response_text:
        logger.error(f"LLM did not return any content for user {user_phone} after multiple retries.")
        raise ValueError("LLM did not return any content after multiple retries.")

    # Parse Pydantic e geração de IDs determinísticos
    # Limpa markdown do JSON se a IA ignorar a instrução de responder apenas texto puro
    json_clean = re.sub(r'```json\s?|\s?```', '', llm_response_text).strip()
    payload = OpenFinancePayload.model_validate_json(json_clean)
    
    final_transactions = []
    for tx in payload.transactions:
        desc_lower = _normalize(tx.description)
        
        # 1. Filtro de segurança: Ignorar pagamentos de fatura
        if "pagamento de fatura" in desc_lower or "pagto fatura" in desc_lower:
            logger.info(f"Ignorando transação de pagamento de fatura: {tx.description}")
            continue

        # 2. Tratar valores negativos como income (caso a LLM tenha extraído o sinal)
        if tx.amount < 0:
            tx.amount = abs(tx.amount)
            tx.type = "income"

        tx.id = _generate_transaction_hash_id(tx, user_phone)

        # Normaliza payment_method para 'credito' ou 'debito' para corresponder às restrições do banco de dados
        if tx.payment_method:
            normalized_method = tx.payment_method.lower()
            if "credito" in normalized_method or "credit" in normalized_method:
                tx.payment_method = "credito"
            elif "debito" in normalized_method or "debit" in normalized_method:
                tx.payment_method = "debito"

        final_transactions.append(tx)

    logger.info(f"Parsed {len(final_transactions)} transactions from LLM output for {user_phone}.")
    return final_transactions
