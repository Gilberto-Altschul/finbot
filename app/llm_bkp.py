# app/llm.py
from __future__ import annotations

import logging
from typing import Any
import asyncio
import random
from google import genai
from google.genai import types, errors
from app.config import get_settings

logger = logging.getLogger(__name__)

# Singleton do cliente GenAI para otimizar conexões
_settings = get_settings()
#modelos Deixamos o SDK gerenciar a versão da API automaticamente para evitar erros 404/400
_client = genai.Client(api_key=_settings.gemini_api_key)

async def call_llm(
    system: str,
    history: list[dict[str, str]],
    message: str,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Chama de forma assíncrona o modelo Gemini adaptando o histórico
    e blindando contra falhas de envio de esquemas de ferramentas.
    """
    try:
        # Converte o histórico de conversação do banco para o formato de Contents aceito pelo SDK
        contents_payload = []
        for msg in history:
            role_map = "user" if msg["role"] == "user" else "model"
            contents_payload.append(
                types.Content(
                    role=role_map,
                    parts=[types.Part.from_text(text=msg["content"])]
                )
            )
        
        # Adiciona a última mensagem do utilizador que disparou o webhook
        contents_payload.append(
            types.Content(role="user", parts=[types.Part.from_text(text=message)])
        )

        # Configura as instruções do sistema
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2, # Temperatura baixa para manter as chamadas a funções precisas
        )

        # Se houver ferramentas disponíveis, converte e injeta de forma compatível
        if tools:
            # Envelopa os dicionários puros de SCHEMAS para o formato nativo da API
            converted_tools = []
            for t in tools:
                converted_tools.append({"function_declarations": [t]})
            config.tools = converted_tools

        # Prioridade para o 1.5-Flash-8B (Lite) devido à cota de 1.500 RPD no Free Tier
        modelos_para_tentar = [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-1.5-flash-8b",
            "gemini-1.5-flash",
            "gemini-1.5-pro"
        ]
        response = None

        for model_name in modelos_para_tentar:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.info(f"Chamando LLM com modelo: {model_name} (Tentativa {attempt+1})")
                    response = await _client.aio.models.generate_content(
                        model=model_name,
                        contents=contents_payload,
                        config=config
                    )
                    if response:
                        logger.info(f"Sucesso com modelo: {model_name}")
                        break
                except (errors.ServerError, errors.ClientError) as exc:
                    # Erros de cota ou instabilidade temporária
                    if attempt < max_retries - 1 and ("429" in str(exc) or "503" in str(exc)):
                        wait_time = (attempt + 1) * 5 + random.uniform(1, 3)
                        logger.warning(f"Cota excedida. Retentativa rápida em {wait_time:.1f}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    logger.warning(f"Troca de modelo: {model_name} falhou. Tentando próximo da lista...")
                    break # Sai do loop de retentativa para trocar o modelo
            
            if response:
                break

        if not response:
            raise Exception("Todos os modelos de IA falharam ou estão sem cota disponível.")

        # Verifica se o modelo decidiu acionar alguma ferramenta mapeada
        if response.function_calls:
            call = response.function_calls[0]
            return {
                "type": "tool_call",
                "tool_calls": [
                    {
                        "name": call.name,
                        "args": dict(call.args) if call.args else {}
                    }
                ]
            }

        # Caso contrário, retorna o texto puro gerado pela IA
        return {
            "type": "text",
            "content": response.text or "Não consegui formular uma resposta para isso."
        }

    except Exception as exc:
        # LINHA CRÍTICA: Esse log vai cuspir no terminal o verdadeiro motivo do erro do Gemini!
        logger.error(f"Erro crítico na chamada da API do Gemini (call_llm): {exc}", exc_info=True)
        # Propaga o erro de volta para o agent.py tratar
        raise exc