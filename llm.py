# llm.py
# ─────────────────────────────────────────────────────────────────────────────
# Unified LLM interface with automatic fallback.
#
# call_llm(payload) → tries providers in order until one succeeds.
#
# Returns:
#   {"type": "text",      "content": str,        "provider": str}
#   {"type": "tool_call", "tool_calls": [...],   "provider": str}
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
from typing import Any

import google.generativeai as genai
from groq import Groq

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Lazy clients ──────────────────────────────────────────────────────────────

_groq_client: Groq | None = None


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
    genai.configure(api_key=settings.gemini_api_key)

    # Convert tools to Gemini function declarations
    gemini_tools = None
    if tools:
        gemini_tools = [
            genai.protos.Tool(
                function_declarations=[
                    genai.protos.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        parameters=genai.protos.Schema(
                            type=genai.protos.Type.OBJECT,
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

    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=system,
        tools=gemini_tools,
    )

    # Convert history to Gemini format
    gemini_history = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [m["content"]],
        }
        for m in history
    ]

    chat = model.start_chat(history=gemini_history)
    response = chat.send_message(message)
    part = response.candidates[0].content.parts[0]

    if hasattr(part, "function_call") and part.function_call.name:
        fc = part.function_call
        return {
            "type": "tool_call",
            "tool_calls": [{"name": fc.name, "args": dict(fc.args)}],
        }

    return {"type": "text", "content": response.text}


def _schema_to_gemini(prop: dict) -> genai.protos.Schema:
    """Convert a simple JSON Schema property to Gemini Schema."""
    type_map = {
        "string": genai.protos.Type.STRING,
        "number": genai.protos.Type.NUMBER,
        "integer": genai.protos.Type.INTEGER,
        "boolean": genai.protos.Type.BOOLEAN,
    }
    kwargs: dict[str, Any] = {"type": type_map.get(prop.get("type", "string"), genai.protos.Type.STRING)}
    if "description" in prop:
        kwargs["description"] = prop["description"]
    if "enum" in prop:
        kwargs["enum"] = prop["enum"]
    return genai.protos.Schema(**kwargs)


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
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
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
                {"name": tc.function.name, "args": json.loads(tc.function.arguments)}
                for tc in choice.tool_calls
            ],
        }

    return {"type": "text", "content": choice.content}


# ── Provider registry + fallback ──────────────────────────────────────────────

_PROVIDERS = [
    ("gemini", lambda: bool(settings.gemini_api_key), _call_gemini),
    ("groq",   lambda: bool(settings.groq_api_key),   _call_groq),
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
            logger.debug(f"Provider {name} skipped (no API key)")
            continue
        try:
            result = await adapter(system, history, message, tools or [])
            logger.info(f"LLM success via {name}", extra={"type": result["type"]})
            return {**result, "provider": name}
        except Exception as exc:
            logger.warning(f"Provider {name} failed: {exc}")
            errors.append(f"{name}: {exc}")

    raise RuntimeError(f"All LLM providers failed → {' | '.join(errors)}")
