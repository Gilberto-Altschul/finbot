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

# Cliente compartilhado para evitar latência de inicialização
_client = genai.Client(api_key=settings.gemini_api_key)

async def converter_texto_c6_para_json_padrao(texto_pdf_cru: str) -> str:
    """
    Usa a interface oficial do Gemini 2.5 para estruturar e normalizar as 
    linhas de texto do PDF do C6 no JSON padrão Open Finance.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."
    user_prompt = f"""
    Sua tarefa é ler o texto bruto de um extrato ou fatura do C6 Bank e convertê-lo em um objeto JSON com a chave 'transactions'.

    Regras de Negócio:
    1. Ignore linhas de cabeçalho, subtotais ou avisos informativos.
    2. Ignore linhas de 'Estorno' (mantenha apenas as despesas reais).
    3. Identifique o nome do banco ('C6 Bank'), o titular e os 4 últimos dígitos do cartão.
    4. Para cada transação encontrada (compras no cartão, pagamentos de boletos, pix ou transferências):
       - 'id': Gere um ID determinístico baseado na origem, data e valor (ex: c6_8525_20260503_19493).
       - 'date': Converta para ISO YYYY-MM-DD. Se a fatura referenciar um ano específico (como 2026), use-o como base.
       - 'description': Nome limpo do estabelecimento (ex: 'SLEEP HOUSE').
       - 'amount': Deve ser um número FLOAT POSITIVO (ex: 194.93).
       - 'category': Mapeie rigorosamente para uma destas: 'Alimentação', 'Transporte', 'Lazer', 'Moradia', 'Saúde', 'Educação' ou 'Outros'.
       - 'payment_method': Identifique se é 'credito' (compras na fatura) ou 'debito' (pagamentos de boletos/contas).
       - 'type': Sempre 'expense'.

    Texto Bruto do Extrato:
    \"\"\"
    {texto_pdf_cru}
    \"\"\"
    """

    try:
        # Força o modelo de forma nativa a responder apenas JSON válido
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = _client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=OpenFinancePayload,
                        temperature=0.1
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

        return response.text.strip()
    except Exception as e:
        logger.error(f"Erro ao chamar o Gemini no pdf_import: {e}")
        raise e