# app/pdf_import.py
import logging
import asyncio
import random
import tempfile
import os
import re # Importar o módulo 're'
import hashlib # Import hashlib
from google import genai
from google.genai import types, errors

# Padrão absoluto para rodar a partir da raiz do projeto:
from app.config import get_settings # type: ignore
from app.ofx_schema import OpenFinancePayload, StandardTransaction # type: ignore

logger = logging.getLogger(__name__)
settings = get_settings()

# Cliente compartilhado para evitar latência de inicialização
_client = genai.Client(api_key=settings.gemini_api_key)

def _generate_transaction_hash_id(transaction: StandardTransaction, user_phone: str) -> str:
    """Generates a deterministic hash ID for a transaction."""
    # Remove apenas padrões de data (dd/mm ou dd-mm) que costumam variar na extração.
    # Mantemos os outros números para diferenciar estabelecimentos similares.
    cleaned_desc = re.sub(r'\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?', '', transaction.description) # Remove datas
    raw_desc = "".join(filter(str.isalnum, cleaned_desc.lower()))
    # Valor com precisão fixa para evitar que 50.0 seja diferente de 50.00 (ex: 50.00 vs 50.0)
    amt_str = "{:.2f}".format(abs(transaction.amount))
    
    # Combina telefone, data, valor e descrição para o hash.
    # Isso garante que a mesma transação em extratos de usuários diferentes tenha IDs únicos no banco.
    unique_string = f"{user_phone}|{transaction.date}|{amt_str}|{raw_desc}|{transaction.type}" 
    return hashlib.sha256(unique_string.encode()).hexdigest()

async def converter_pdf_nativo_para_json(pdf_content: bytes, user_phone: str) -> str:
    """
    Faz o upload do PDF para o Google e usa a visão computacional do Gemini
    para extrair as transações com precisão de layout.
    """
    system_prompt = "Você é um microsserviço de backend especialista em processamento de dados financeiros."
    user_instructions = """
    Analise o extrato do C6 Bank anexo e converta-o em um objeto JSON com a chave 'transactions'.

    Regras de Negócio:
    1. Ignore linhas de cabeçalho, subtotais ou avisos informativos.
    2. Ignore linhas de 'Estorno' (mantenha apenas as despesas reais).
    3. Identifique o nome do banco ('C6 Bank'), o titular e os 4 últimos dígitos do cartão.
    4. Para cada transação encontrada (compras no cartão, pagamentos de boletos, pix ou transferências):
       - 'id': Gere um ID *temporário* baseado na origem, data e valor (ex: c6_8525_20260503_19493). Este ID será sobrescrito pelo sistema.
       - 'date': Converta para ISO YYYY-MM-DD. Se a fatura referenciar um ano específico (como 2026), use-o como base.
       - 'description': Nome limpo do estabelecimento (ex: 'SLEEP HOUSE').
       - 'amount': Deve ser um número FLOAT POSITIVO (ex: 194.93).
       - 'category': Mapeie rigorosamente para uma destas: 'Alimentação', 'Transporte', 'Lazer', 'Moradia', 'Saúde', 'Vestuário e Beleza', 'Educação', 'Pets', 'Financeiro', 'Extra' ou 'Outros'. (IMPORTANTE: Planos de Saúde ou Convênios são 'Saúde', NUNCA 'Financeiro').
       - 'subcategory': Mapeie para uma subcategoria lógica (ex: 'Mercado', 'Combustível', 'Streaming', 'Farmácia').
       - 'installment_of': Se for uma compra parcelada (ex: 02/10), extraia o número da parcela atual (2).
       - 'installment_total': Se for uma compra parcelada (ex: 02/10), extraia o total de parcelas (10).
       - 'payment_method': Identifique se é 'credito' (compras na fatura) ou 'debito' (pagamentos de boletos/contas).
       - 'type': Identifique se é 'expense' (despesa) ou 'income' (receita).

    5. SEÇÃO DE PARCELAMENTOS: Procure por áreas intituladas 'Parcelamentos' ou 'Compras Parceladas'. Elas listam as parcelas de compras passadas que estão sendo cobradas nesta fatura. Extraia cada uma como uma transação individual seguindo as regras acima.

    REGRAS ESPECÍFICAS:
    - Transações com a descrição 'PIX TRANSF ERNST' devem ser obrigatoriamente classificadas como 'type': 'income' e 'category': 'Extra'.
    """

    tmp_path = None
    try:
        # 1. Cria um arquivo temporário seguro para o upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_content)
            tmp_path = tmp.name

        # 2. Sobe para o Google AI File API (Multimodal)
        google_file = await _client.aio.files.upload(file=tmp_path)

        max_retries = 3
        llm_response_text = None
        
        # Prioriza 1.5 Flash para processamento massivo de PDF com risco zero
        modelos_para_tentar = [
            "gemini-3.5-flash",          # Mais recente e rápido para multimodal
            "gemini-3.1-flash",          # Boa alternativa da série 3.x
            "gemini-3.1-flash-lite",     # Versão mais leve da série 3.x
            "gemini-2.5-flash",          # Próxima geração Flash
            "gemini-2.5-flash-lite",     # Sua customização (cota baixa, mas pode ser útil)
            "gemini-1.5-flash",          # Cavalo de batalha, muito estável
            "gemini-3.1-pro",            # Mais capaz, mas mais lento/custoso (último recurso)
            "gemini-2.5-pro"             # Similar ao 3.1 Pro
        ]
        
        for model_name in modelos_para_tentar:
            logger.info(f"Tentando extração com modelo: {model_name}")
            for attempt in range(max_retries):
                try:
                    response = await _client.aio.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_uri(file_uri=google_file.uri, mime_type="application/pdf"),
                                    types.Part.from_text(text=user_instructions)
                                ]
                            )
                        ],
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            response_schema=OpenFinancePayload,
                            temperature=0.1
                        )
                    )
                    llm_response_text = response.text.strip()
                    if llm_response_text:
                        break 
                except (errors.ServerError, errors.ClientError) as exc:
                    # Erros 503/429 são temporários; outros (400, etc) são fatais
                    if attempt < max_retries - 1 and ("503" in str(exc) or "429" in str(exc)):
                        wait_time = (2 ** attempt) * 5 + random.uniform(0, 1)
                        logger.warning(f"Modelo {model_name} ocupado. Retentativa {attempt+1} em {wait_time:.1f}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    logger.error(f"Modelo {model_name} falhou ou retornou erro fatal: {exc}")
                    break # Pula para o próximo modelo da lista
            
            if llm_response_text:
                break

        # If LLM response text is empty after retries, raise an error
        if not llm_response_text:
            raise ValueError("LLM did not return any content after multiple retries.")

        # Parse the LLM response into a Pydantic model
        payload = OpenFinancePayload.model_validate_json(llm_response_text)

        # Generate deterministic IDs for each transaction
        for tx in payload.transactions:
            tx.id = _generate_transaction_hash_id(tx, user_phone)
        
        # Return the modified payload as a JSON string
        return payload.model_dump_json() # Use model_dump_json for Pydantic v2
    except Exception as e:
        logger.error(f"Erro ao chamar o Gemini no pdf_import: {e}")
        raise e
    finally:
        # Limpeza do arquivo temporário
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)