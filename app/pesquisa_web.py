"""Pesquisa na web (Gemini + Google Search) sobre um estabelecimento desconhecido do extrato.

Só o NOME do estabelecimento sai daqui (nada de valor, data ou dados do usuário). O resultado é uma frase curta usada
como contexto da classificação — nunca vira regra sozinho: o palpite continua passando pela revisão do usuário.
Desligue com FINBOT_PESQUISA_WEB=0. Qualquer falha (sem chave, sem permissão de busca, timeout) devolve None
e a classificação segue como antes."""
import asyncio
import logging
import os
import re
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


_MESES = r"(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)"
_RUIDO = [
    rf"\b{_MESES}\s*/\s*\d{{2,4}}\b",              # "OUT/26": mês de cobrança, não faz parte do nome
    r"\b\d{1,2}\s*/\s*\d{1,2}(?:\s*/\s*\d{2,4})?\b",  # datas e parcelas "02/03"
    r"\b(?:compra\s+)?(?:debito|credito)\b",
    r"\bparc(?:ela)?\b",
]


def limpar_descricao(descricao: str) -> str:
    """Tira do texto do extrato o que atrapalha a busca (mês de cobrança, parcela, '*', hífens soltos)."""
    t = (descricao or "").lower()
    for padrao in _RUIDO:
        t = re.sub(padrao, " ", t)
    t = re.sub(r"[*_]+", " ", t)
    t = re.sub(r"\s[-–]+\s|\s[-–]+$|^[-–]+\s", " ", t)
    return " ".join(t.split()).strip(" -–")


def _prompt(descricao: str, segunda_tentativa: bool = False) -> str:
    base = (
        f"Em uma frase curta (no máximo 20 palavras, em português), diga que tipo de estabelecimento ou serviço é "
        f"\"{descricao}\". O texto vem de um extrato de cartão de crédito no Brasil (provavelmente São Paulo). "
    )
    if segunda_tentativa:
        base += (
            "Pode ser uma cobrança recorrente (seguro, assinatura, tarifa ou serviço do banco ou do cartão), um sebo "
            "(livraria de usados) ou outro comércio de bairro; considere também o nome abreviado. "
        )
    return base + "Responda só com a frase. Se não tiver certeza do que é, responda exatamente: DESCONHECIDO"


async def _consultar(descricao: str, segunda_tentativa: bool = False) -> str:
    from google.genai import types
    resp = await _cliente().aio.models.generate_content(
        model=MODELO, contents=_prompt(descricao, segunda_tentativa),
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
    consulta = limpar_descricao(descricao) or descricao
    valor = None
    try:
        for segunda in (False, True):   # se a 1ª não reconhecer o lugar, tenta de novo com mais contexto
            texto = await asyncio.wait_for(_consultar(consulta, segunda) if segunda else _consultar(consulta),
                                           timeout=TIMEOUT_S)
            texto = " ".join(texto.split())[:240]
            if texto and "DESCONHECIDO" not in texto.upper():
                valor = texto
                break
    except Exception as e:
        logger.warning(f"Pesquisa web indisponível para '{descricao}': {e}")
        _cache[chave] = (agora, None)
        return None
    _cache[chave] = (agora, valor)
    if valor:
        logger.info(f"Pesquisa web: '{descricao}' (busca: '{consulta}') -> {valor}")
    else:
        logger.info(f"Pesquisa web: '{descricao}' (busca: '{consulta}') -> desconhecido; vai para revisão")
    return valor
