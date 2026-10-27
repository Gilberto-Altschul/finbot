"""Pesquisa na web (Gemini + Google Search) sobre um estabelecimento desconhecido do extrato.

Só o NOME do estabelecimento sai daqui (nada de valor, data ou dados do usuário). O resultado é uma frase curta usada
como contexto da classificação — nunca vira regra sozinho: o palpite continua passando pela revisão do usuário.
Desligue com FINBOT_PESQUISA_WEB=0. Qualquer falha (sem chave, sem permissão de busca, timeout) devolve None
e a classificação segue como antes."""
import asyncio
import logging
import os
import time

from app.utils import _normalize

logger = logging.getLogger(__name__)

MODELO = os.getenv("FINBOT_PESQUISA_MODELO", "gemini-2.5-flash")
TIMEOUT_S = float(os.getenv("FINBOT_PESQUISA_TIMEOUT", "12"))
_TTL_FALHA_S = 3600          # não insiste em falha por 1 h
_cache: dict[str, tuple[float, str | None]] = {}
_client = None


def ativa() -> bool:
    return os.getenv("FINBOT_PESQUISA_WEB", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


def _cliente():
    global _client
    if _client is None:
        from google import genai
        from app.config import get_settings
        _client = genai.Client(api_key=get_settings().gemini_api_key)
    return _client


def _prompt(descricao: str) -> str:
    return (
        f"Em uma frase curta (no máximo 20 palavras, em português), diga que tipo de estabelecimento ou serviço é "
        f"\"{descricao}\". O texto vem de um extrato de cartão de crédito no Brasil (provavelmente São Paulo). "
        f"Responda só com a frase. Se não tiver certeza do que é, responda exatamente: DESCONHECIDO"
    )


async def _consultar(descricao: str) -> str:
    from google.genai import types
    resp = await _cliente().aio.models.generate_content(
        model=MODELO, contents=_prompt(descricao),
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.0))
    return (getattr(resp, "text", None) or "").strip()


async def descrever(descricao: str) -> str | None:
    """Frase curta sobre o estabelecimento, ou None (desligado, desconhecido ou falha)."""
    if not ativa() or not (descricao or "").strip():
        return None
    chave = _normalize(descricao)
    agora = time.time()
    if chave in _cache:
        quando, valor = _cache[chave]
        if valor is not None or agora - quando < _TTL_FALHA_S:
            return valor
    try:
        texto = await asyncio.wait_for(_consultar(descricao), timeout=TIMEOUT_S)
    except Exception as e:
        logger.warning(f"Pesquisa web indisponível para '{descricao}': {e}")
        _cache[chave] = (agora, None)
        return None
    texto = " ".join(texto.split())[:240]
    valor = None if (not texto or "DESCONHECIDO" in texto.upper()) else texto
    _cache[chave] = (agora, valor)
    if valor:
        logger.info(f"Pesquisa web: '{descricao}' -> {valor}")
    return valor
