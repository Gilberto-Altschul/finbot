# llm.py
# ─────────────────────────────────────────────────────────────────────────────
# Unified LLM interface with automatic fallback.
#
# call_llm(payload) → tries providers in order until one succeeds.
#
# Returns:
# {"type": "text", "content": str, "provider": str}
# {"type": "tool_call", "tool_calls": [...], "provider": str}
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from google import genai
from google.genai import types

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Clients ───────────────────────────────────────────────────────────────────

_gemini_client: genai.Client | None = None


def _gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


def _schema_to_gemini(prop: dict) -> types.Schema:
    type_map = {
        "string": "STRING", "number": "NUMBER", "integer": "INTEGER",
        "boolean": "BOOLEAN", "object": "OBJECT", "array": "ARRAY",
    }
    kwargs: dict[str, Any] = {"type": type_map.get(prop.get("type", "").lower(), "STRING")}
    if "description" in prop:
        kwargs["description"] = prop["description"]
    if "enum" in prop:
        kwargs["enum"] = prop["enum"]
    return types.Schema(**kwargs)


# ── Gemini adapter (accepts model param) ─────────────────────────────────────

async def _call_gemini_model(
    model: str,
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict],
) -> dict:
    client = _gemini()

    gemini_tools = None
    if tools:
        gemini_tools = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters=types.Schema(
                            type="OBJECT",
                            properties={
                                k: _schema_to_gemini(v)
                                for k, v in t["parameters"].get("properties", {}).items()
                            },
                            required=t["parameters"].get("required", []),
                        ),
                    )
                    for t in tools
                ]
            )
        ]

    gemini_history = [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part(text=m["content"])],
        )
        for m in history
    ]

    chat = client.aio.chats.create(
        model=model,
        history=gemini_history,
        config=types.GenerateContentConfig(
            system_instruction=types.Content(parts=[types.Part(text=system)]),
            tools=gemini_tools,
            tool_config=(
                types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="AUTO")
                )
                if gemini_tools else None
            ),
        ),
    )

    response = await chat.send_message(message)

    if not response.candidates or not response.candidates[0].content.parts:
        return {"type": "text", "content": "Desculpe, não consegui processar sua mensagem."}

    candidate = response.candidates[0]
    tool_calls = []
    for part in candidate.content.parts:
        if getattr(part, "function_call", None):
            tool_calls.append({
                "name": part.function_call.name,
                "args": dict(part.function_call.args or {}),
            })

    if tool_calls:
        return {"type": "tool_call", "tool_calls": tool_calls}
    return {"type": "text", "content": response.text}


async def _call_gemini(system, history, message, tools):
    return await _call_gemini_model("gemini-2.5-flash-lite", system, history, message, tools)

async def _call_gemini_flash(system, history, message, tools):
    """Gemini 2.0 Flash — cota separada do 2.5 Flash Lite."""
    return await _call_gemini_model("gemini-2.0-flash", system, history, message, tools)


# ── Groq adapter ──────────────────────────────────────────────────────────────

async def _call_groq(
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict],
) -> dict:
    if not settings.groq_api_key:
        raise RuntimeError("Groq API key not configured")

    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": message},
    ]

    groq_tools = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in tools
    ] if tools else None

    payload: dict[str, Any] = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 600,
    }
    if groq_tools:
        payload["tools"] = groq_tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Groq {response.status_code}: {response.text[:200]}")
        data = response.json()

    choice = data["choices"][0]["message"]
    if choice.get("tool_calls"):
        return {
            "type": "tool_call",
            "tool_calls": [
                {"name": tc["function"]["name"], "args": json.loads(tc["function"]["arguments"])}
                for tc in choice["tool_calls"]
            ],
        }
    return {"type": "text", "content": choice["content"]}


# ── OpenRouter adapter ────────────────────────────────────────────────────────
# OpenRouter exposes an OpenAI-compatible API with many free models.
# Default model: meta-llama/llama-3.3-70b-instruct (free)

OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


async def _call_openrouter(
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict],
) -> dict:
    logger.info(f"Iniciando chamada de fallback ao OpenRouter (Modelo: {OPENROUTER_MODEL})")
    
    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": message},
    ]

    openrouter_tools = (
        [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        if tools
        else None
    )

    payload: dict[str, Any] = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 600,
    }

    if openrouter_tools:
        payload["tools"] = openrouter_tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://finbot.app",
                "X-Title": "FinBot",
            },
            json=payload,
        )
        
        if response.status_code >= 400:
            error_detail = response.text
            logger.error(f"OpenRouter API Error {response.status_code}: {error_detail}")
            raise RuntimeError(f"OpenRouter {response.status_code}: {error_detail}")

        if response.is_error:
            logger.error(f"Erro detalhado do OpenRouter ({response.status_code}): {response.text}")

        response.raise_for_status()
        data = response.json()

        # Log de monitoramento de limites (opcional para debug)
        limit_rem = response.headers.get("x-ratelimit-remaining-requests")
        if limit_rem:
            logger.info(f"OpenRouter - Requisições restantes: {limit_rem}")

    choice = data["choices"][0]["message"]

    if choice.get("tool_calls"):
        return {
            "type": "tool_call",
            "tool_calls": [
                {
                    "name": tc["function"]["name"],
                    "args": json.loads(tc["function"]["arguments"]),
                }
                for tc in choice["tool_calls"]
            ],
        }

    return {"type": "text", "content": choice["content"]}


# ── Provider registry + fallback ──────────────────────────────────────────────
# Order: Gemini 2.5 Flash Lite → Gemini 2.0 Flash → OpenRouter → Groq
# Each has its own quota — if one hits 429, next one takes over automatically.

_PROVIDERS = [
    ("gemini-2.5-flash-lite", lambda: bool(settings.gemini_api_key),      _call_gemini),
    ("gemini-2.0-flash",      lambda: bool(settings.gemini_api_key),      _call_gemini_flash),
    ("openrouter",            lambda: bool(settings.openrouter_api_key),  _call_openrouter),
    ("groq",                  lambda: bool(settings.groq_api_key),        _call_groq),
]


async def call_llm(
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict] | None = None,
) -> dict:
    """
    Call the LLM with automatic fallback.
    Returns a dict with keys: type, content/tool_calls, provider.
    """
    errors: list[str] = []

    for name, is_enabled, adapter in _PROVIDERS:
        if not is_enabled():
            logger.warning(f"Provider {name} ignorado: chave não encontrada no .env")
            continue

        try:
            result = await adapter(system, history, message, tools or [])
            logger.info(
                f"LLM success via {name} | type={result['type']} | result={result}"
            )
            return {**result, "provider": name}
        except Exception as exc:
            error_msg = str(exc)
            if "401" in error_msg or "Unauthorized" in error_msg:
                error_msg = "Invalid API Key"

            logger.warning(f"Provider {name} failed: {error_msg}")
            errors.append(f"{name} ({error_msg})")

    raise RuntimeError(f"All LLM providers failed → {' | '.join(errors)}")