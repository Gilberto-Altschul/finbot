# llm.py
# ─────────────────────────────────────────────────────────────────────────────
# Unified LLM interface with automatic fallback.
#
# call_llm(payload) → tries providers in order until one succeeds.
#
# Returns:
#   {"type": "text",      "content": str,         "provider": str}
#   {"type": "tool_call", "tool_calls": [...],    "provider": str}
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
from typing import Any

from google import genai
from google.genai import types
from groq import Groq
import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Clients Configuration ─────────────────────────────────────────────────────

_gemini_client: genai.Client | None = None
_groq_client: Groq | None = None


def _gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


def _groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=settings.groq_api_key)
    return _groq_client


# ── Gemini adapter ────────────────────────────────────────────────────────────

async def _call_gemini(
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
        model="gemini-2.5-flash-lite",
        history=gemini_history,
        config=types.GenerateContentConfig(
            system_instruction=types.Content(parts=[types.Part(text=system)]),
            tools=gemini_tools,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                  mode="ANY"
                )
            ) if gemini_tools else None,
         ),
    )    
    response = await chat.send_message(message)
    candidate = response.candidates[0]

    for part in candidate.content.parts:
        if getattr(part, "function_call", None):
            return {
                "type": "tool_call",
                "tool_calls": [
                    {
                        "name": part.function_call.name,
                        "args": part.function_call.args,
                    }
                ],
            }

    return {"type": "text", "content": response.text}


def _schema_to_gemini(prop: dict) -> types.Schema:
    """Convert a simple JSON Schema property to Gemini Schema."""
    type_map = {
        "string": "STRING",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "object": "OBJECT",
        "array": "ARRAY",
    }

    kwargs: dict[str, Any] = {
        "type": type_map.get(prop.get("type", "").lower(), "STRING")
    }

    if "description" in prop:
        kwargs["description"] = prop["description"]
    if "enum" in prop:
        kwargs["enum"] = prop["enum"]

    return types.Schema(**kwargs)


# ── Groq adapter ──────────────────────────────────────────────────────────────

async def _call_groq(
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict],
) -> dict:
    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": message},
    ]

    groq_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ] if tools else None

    completion = _groq().chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        tools=groq_tools,
        tool_choice="auto" if groq_tools else None,
        temperature=0.2,
        max_tokens=600,
    )

    choice = completion.choices[0].message

    if choice.tool_calls:
        return {
            "type": "tool_call",
            "tool_calls": [
                {
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments),
                }
                for tc in choice.tool_calls
            ],
        }

    return {"type": "text", "content": choice.content}


# ── OpenRouter adapter ────────────────────────────────────────────────────────
# OpenRouter exposes an OpenAI-compatible API with many free models.
# Default model: meta-llama/llama-3.3-70b-instruct (free)

OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"


async def _call_openrouter(
    system: str,
    history: list[dict],
    message: str,
    tools: list[dict],
) -> dict:
    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": message},
    ]

    openrouter_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ] if tools else None

    payload: dict = {
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
        response.raise_for_status()
        data = response.json()

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

_PROVIDERS = [
    ("gemini",      lambda: bool(settings.gemini_api_key),      _call_gemini),
    ("openrouter",  lambda: bool(settings.openrouter_api_key),  _call_openrouter),
    ("groq",        lambda: bool(settings.groq_api_key),        _call_groq),
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
            logger.warning(f"Provider {name} skipped: Missing API key in .env")
            continue

        try:
            result = await adapter(system, history, message, tools or [])
            logger.info(f"LLM success via {name} | type={result['type']} | result={result}")
            return {**result, "provider": name}
        except Exception as exc:
            logger.warning(f"Provider {name} failed: {exc}")
            errors.append(f"{name}: {exc}")

    raise RuntimeError(f"All LLM providers failed → {' | '.join(errors)}")