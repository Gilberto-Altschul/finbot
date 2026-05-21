# app/llm.py
from __future__ import annotations

import logging
from typing import Any
import asyncio
from google import genai
from google.genai import types, errors
from app.config import get_settings

logger = logging.getLogger(__name__)

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
    settings = get_settings()
    
    try:
        # Inicializa o cliente oficial do SDK da Google utilizando a sua chave do .env
        client = genai.Client(api_key=settings.gemini_api_key)
        
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

        # Faz a chamada síncrona dentro da thread para o modelo recomendado para ações rápidas
        # Altere para "gemini-2.5-flash" se já estiver utilizando o ambiente atualizado
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=contents_payload,
                    config=config,
                )
                break
            except (errors.ServerError, errors.ClientError) as exc:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5
                    logger.warning(f"Gemini API indisponível (503/429). Tentando novamente em {wait_time}s... ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
                raise exc

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