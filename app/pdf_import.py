# app/pdf_import.py
import logging
import asyncio
import random
import re
import hashlib
import io
import pdfplumber
from google import genai
from google.genai import types, errors

from app.utils import _normalize
from app.config import get_settings
from app.ofx_schema import OpenFinancePayload, StandardTransaction

logger = logging.getLogger(__name__)
settings = get_settings()

_client = genai.Client(api_key=settings.gemini_api_key)


def _generate_transaction_hash_id(transaction: StandardTransaction, user_phone: str) -> str:
    """Gera ID determinístico para a transação pelo conteúdo."""
    normalized_desc = _normalize(transaction.description)
    amt_str = "{:.2f}".format(abs(transaction.amount))
    unique_string = f"{user_phone}|{transaction.date}|{amt_str}|{normalized_desc}|{transaction.type}"
    return hashlib.sha256(unique_string.encode()).hexdigest()


def _extrair_texto_pdf(pdf_content: bytes) -> str:
    """
    Extrai texto do PDF de forma determinística usando pdfplumber.
    Retorna o texto completo de todas as páginas.
    """
    texto_paginas = []
    with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
        for i, page in enumerate(pdf.pages):
            texto = page.extract_text(layout=True)  # layout=True preserva colunas
            if texto:
                texto_paginas.append(f"=== PÁGINA {i+1} ===\n{texto}")
    
    texto_completo = "\n\n".join(texto_paginas)
    logger.info(f"pdfplumber extraiu {len(texto_completo)} chars de {len(pdf.pages) if hasattr(pdf, 'pages') else '?'} páginas")
    return texto_completo


async def converter_pdf_nativo_para_json(pdf_content: bytes, user_phone: str) -> list[StandardTransaction]:
    """
    Extrai texto do PDF com pdfplumber (determinístico) e envia para o Gemini estruturar em JSON.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."

    # 1. Extrai texto de forma determinística
    try:
        texto_pdf = _extrair_texto_pdf(pdf_content)
        logger.info(f"Primeiros 500 chars do texto:\n{texto_pdf[:500]}")
    except Exception as e:
        logger.error(f"Erro ao extrair texto com pdfplumber: {e}. Tentando fallback com PDF nativo.")
        texto_pdf = None

    # 2. Detecta mês/ano da fatura a partir do texto extraído
    fatura_mes = None
    fatura_ano = None
    if texto_pdf:
        # Padrões comuns: "vencimento 01/06/2026", "25/04/26 a 26/05/26", "junho/2026", "jun/26"
        MESES_PT = {
            'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
            'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
        }
        # Padrão: DD/MM/YYYY após "vencimento"
        m = re.search(r'vencimento\D{0,10}(\d{2})/(\d{2})/(\d{4})', texto_pdf, re.IGNORECASE)
        if m:
            fatura_mes = int(m.group(2))
            fatura_ano = int(m.group(3))
        # Padrão Santander: "Esta Fatura DD/MM/YY a DD/MM/YY"
        if not fatura_mes:
            m = re.search(r'Esta Fatura\s+\d{2}/\d{2}/\d{2}\s+a\s+(\d{2})/(\d{2})/(\d{2})', texto_pdf, re.IGNORECASE)
            if m:
                fatura_mes = int(m.group(2))
                fatura_ano = 2000 + int(m.group(3))
        # Padrão: "junho/2026" ou "jun/2026"
        if not fatura_mes:
            m = re.search(r'(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*/(\d{4})', texto_pdf, re.IGNORECASE)
            if m:
                fatura_mes = MESES_PT.get(m.group(1).lower()[:3])
                fatura_ano = int(m.group(2))
        # Detecta dia de vencimento
        fatura_dia = None
        m_dia = re.search(r'vencimento\D{0,10}(\d{2})/(\d{2})/(\d{4})', texto_pdf, re.IGNORECASE)
        if m_dia:
            fatura_dia = int(m_dia.group(1))

        if fatura_mes and fatura_ano:
            billing_date = f"{fatura_ano}-{fatura_mes:02d}-{fatura_dia:02d}" if fatura_dia else f"{fatura_ano}-{fatura_mes:02d}-01"
            logger.info(f"Fatura detectada: vencimento {billing_date}")
        else:
            # Fallback: usa mês/ano atual
            from datetime import datetime
            now = datetime.now()
            fatura_mes = now.month
            fatura_ano = now.year
            fatura_dia = now.day
            billing_date = now.strftime("%Y-%m-%d")
            logger.warning(f"Não detectou mês/ano da fatura. Usando atual: {billing_date}")

    # 3. Monta o conteúdo para o Gemini
    if texto_pdf and len(texto_pdf.strip()) > 100:
        logger.info("Usando abordagem híbrida: texto pdfplumber → Gemini")
        user_instructions = f"""
Analise o texto de extrato bancário abaixo e converta-o em um objeto JSON com a chave 'transactions'.

INFORMAÇÃO IMPORTANTE: Esta fatura é do mês {fatura_mes:02d}/{fatura_ano}.

ATENÇÃO: O extrato pode ter duas colunas de transações lado a lado (esquerda e direita).
Leia e extraia TODAS as transações de AMBAS as colunas. Não ignore nenhuma coluna ou seção.

Regras:
1. Ignore linhas de cabeçalho, subtotais, saldos do dia e avisos.
2. Ignore linhas de Estorno.
3. Ignore transações de "Pagamento de fatura", "Pagto fatura" ou similares.
4. Para cada transação (compras, boletos, pix, transferências):
   - id: ID temporário baseado na data e valor (ex: itau_20260503_1950)
   - date: data ORIGINAL da compra no formato ISO YYYY-MM-DD.
     As datas aparecem no formato DD/MM (dia/mês) — NUNCA interprete como MM/DD.
     Exemplo: "03/05" = dia 03 de maio, NÃO dia 05 de março.
     A fatura é do mês {fatura_mes:02d}/{fatura_ano}. Use estas regras para o ano:
     - Se o mês da compra for MAIOR que o mês da fatura ({fatura_mes}) → ano = {fatura_ano - 1}
     - Se o mês da compra for MENOR ou IGUAL ao mês da fatura ({fatura_mes}) → ano = {fatura_ano}
     Exemplos concretos para esta fatura (mês {fatura_mes}/{fatura_ano}):
       - compra em "19/05" ou "19 mai" → mês 05 ≤ {fatura_mes} → ano {fatura_ano} → date: {fatura_ano}-05-19
       - compra em "27/02" ou "27 fev" → mês 02 ≤ {fatura_mes} → ano {fatura_ano} → date: {fatura_ano}-02-27
       - compra em "17/03" ou "17 mar" → mês 03 ≤ {fatura_mes} → ano {fatura_ano} → date: {fatura_ano}-03-17
       - compra em "26/12" ou "26 dez" → mês 12 > {fatura_mes} → ano {fatura_ano - 1} → date: {fatura_ano - 1}-12-26
       - compra em "08/07" ou "08 jul" → mês 07 > {fatura_mes} → ano {fatura_ano - 1} → date: {fatura_ano - 1}-07-08
     NUNCA use ano {fatura_ano + 1} — todas as compras são de {fatura_ano - 1} ou {fatura_ano}.
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
- Valores negativos no extrato → type: income, amount positivo
- Responda APENAS com o JSON, sem markdown.

=== TEXTO DO EXTRATO ===
{texto_pdf}
"""
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_instructions)]
            )
        ]
    else:
        # Fallback: envia PDF como binário (comportamento anterior)
        import base64
        logger.warning("pdfplumber não extraiu texto suficiente. Usando fallback com PDF binário.")
        user_instructions = """
Analise o extrato bancário anexo e converta-o em um objeto JSON com a chave 'transactions'.

ATENÇÃO: O extrato pode ter duas colunas de transações lado a lado (esquerda e direita).
Leia e extraia TODAS as transações de AMBAS as colunas. Não ignore nenhuma coluna ou seção.

Regras:
1. Ignore linhas de cabeçalho, subtotais, saldos do dia e avisos.
2. Ignore linhas de Estorno.
3. Ignore transações de "Pagamento de fatura", "Pagto fatura" ou similares.
4. Para cada transação:
   - id: ID temporário baseado na data e valor
   - date: data ORIGINAL da compra ISO YYYY-MM-DD. Formato DD/MM, NUNCA MM/DD.
   - description: nome limpo do estabelecimento
   - amount: float positivo
   - category: uma de: Alimentação, Transporte, Lazer, Moradia, Saúde, Vestuário e Beleza, Educação, Pets, Financeiro, Extra, Outros
   - subcategory: subcategoria lógica
   - installment_of: número da parcela atual (null se não parcelado)
   - installment_total: total de parcelas (null se não parcelado)
   - payment_method: credito ou debito
   - type: expense ou income
- Responda APENAS com o JSON, sem markdown.
"""
        pdf_b64 = base64.b64encode(pdf_content).decode()
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf_b64)),
                    types.Part.from_text(text=user_instructions),
                ]
            )
        ]

    # 3. Modelos em ordem de preferência
    models_to_try = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash-lite",
    ]

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
                        logger.warning(f"⚠️ Resposta de {model_name} truncada (MAX_TOKENS). Tentando próximo modelo.")
                        llm_response_text = None
                        break
                    logger.info(f"Extração bem-sucedida com {model_name}")
                    break
            except Exception as exc:
                err_str = str(exc)
                is_retryable = any(code in err_str for code in ["429", "503", "Resource has been exhausted"])
                if attempt < 2 and is_retryable:
                    wait = (attempt + 1) * 15 + random.uniform(5, 15)
                    logger.warning(f"⚠️ {model_name} ocupado. Retentativa {attempt+1} em {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Modelo {model_name} falhou: {exc}")
                break

        if llm_response_text:
            break

    if not llm_response_text:
        logger.error(f"LLM did not return any content for user {user_phone} after multiple retries.")
        raise ValueError("LLM did not return any content after multiple retries.")

    # 4. Parse e geração de IDs determinísticos
    json_clean = re.sub(r'```json\s?|\s?```', '', llm_response_text).strip()
    payload = OpenFinancePayload.model_validate_json(json_clean)

    final_transactions = []
    for tx in payload.transactions:
        desc_lower = _normalize(tx.description)

        if any(p in desc_lower for p in [
            "pagamento de fatura", "pagto fatura", "pag fatura",
            "pagamento fatura", "payment", "pag. fatura"
        ]):
            logger.info(f"Ignorando pagamento de fatura: {tx.description}")
            continue

        if tx.amount < 0:
            tx.amount = abs(tx.amount)
            tx.type = "income"

        # Corrige anos futuros — nenhuma compra pode ser de ano posterior à fatura
        if fatura_ano and tx.date:
            try:
                parts = tx.date.split('-')
                tx_ano = int(parts[0])
                tx_mes = int(parts[1])
                tx_dia = int(parts[2])
                if tx_ano > fatura_ano:
                    # Ano futuro — corrige para ano da fatura ou anterior
                    tx_ano_correto = fatura_ano if tx_mes <= fatura_mes else fatura_ano - 1
                    tx.date = f"{tx_ano_correto}-{tx_mes:02d}-{tx_dia:02d}"
                    logger.warning(f"Ano futuro corrigido: {tx.description} {parts[0]} → {tx_ano_correto}")
            except Exception:
                pass

        tx.id = _generate_transaction_hash_id(tx, user_phone)

        # Define billing_date como vencimento da fatura para crédito
        tx.billing_date = billing_date

        if tx.payment_method:
            normalized_method = tx.payment_method.lower()
            if "credito" in normalized_method or "credit" in normalized_method:
                tx.payment_method = "credito"
            elif "debito" in normalized_method or "debit" in normalized_method:
                tx.payment_method = "debito"
            else:
                # boleto, pix, ted, doc → debito
                tx.payment_method = "debito"

        final_transactions.append(tx)

    logger.info(f"Parsed {len(final_transactions)} transactions from LLM output for {user_phone}.")
    return final_transactions
