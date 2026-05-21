# app/pdf_import.py
import logging
import asyncio
from google import genai
from google.genai import types, errors

# Padrão absoluto para rodar a partir da raiz do projeto:
from app.config import get_settings
from app.ofx_schema import OpenFinancePayload

logger = logging.getLogger(__name__)
settings = get_settings()

async def converter_texto_c6_para_json_padrao(texto_pdf_cru: str) -> str:
    """
    Usa a interface oficial do Gemini 2.5 para estruturar e normalizar as 
    linhas de texto do PDF do C6 no JSON padrão Open Finance.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."
    user_prompt = f"""
    Sua tarefa é ler o texto bruto de uma fatura do C6 Bank e convertê-lo em um objeto JSON com uma chave raiz chamada 'transactions'.

    Regras de Negócio:
    1. Ignore linhas de cabeçalho, subtotais ou avisos informativos.
    2. Ignore linhas de 'Estorno' (mantenha apenas as despesas reais).
    3. Identifique o nome do banco ('C6 Bank'), o titular e os 4 últimos dígitos do cartão.
    4. Para cada transação de despesa/gasto:
       - 'id': Gere um ID determinístico no formato 'c6_8525_data_valor_reduzido' (ex: c6_8525_20260503_19493).
       - 'date': Converta para ISO YYYY-MM-DD. Se a fatura referenciar um ano específico (como 2026), use-o como base.
       - 'description': Nome limpo do estabelecimento (ex: 'SLEEP HOUSE').
       - 'amount': Deve ser um número FLOAT POSITIVO (ex: 194.93).
       - 'category': Mapeie rigorosamente para uma destas: 'Alimentação', 'Transporte', 'Lazer', 'Moradia', 'Saúde', 'Educação' ou 'Outros'.
       - 'payment_method': Sempre 'credito'.
       - 'type': Sempre 'expense'.

    Texto Bruto do Extrato:
    \"\"\"
    {texto_pdf_cru}
    \"\"\"
    """

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        
        # Força o modelo de forma nativa a responder apenas JSON válido
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json"
                    )
                )
                break
            except (errors.ServerError, errors.ClientError) as exc:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5
                    logger.warning(f"Erro na extração de PDF (Gemini 503/429). Tentando novamente em {wait_time}s... ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
                raise exc

        text = response.text.strip()
        # Limpeza robusta: remove blocos de código markdown (ex: ```json ... ```) 
        # que a IA pode inserir mesmo com a instrução de mime_type
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text
    except Exception as e:
        logger.error(f"Erro ao chamar o Gemini no pdf_import: {e}")
        raise e