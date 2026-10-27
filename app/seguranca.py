"""
Segurança do webhook do WhatsApp (Twilio).

1. Assinatura: toda chamada legítima da Twilio traz X-Twilio-Signature (HMAC do URL + parâmetros com o
   Auth Token). Sem validar, qualquer pessoa que saiba o endereço do app pode fingir ser qualquer número.
2. Mídia: o app baixa o PDF mandando as credenciais da Twilio no cabeçalho; só pode fazer isso para
   endereços da própria Twilio (senão, quem forjar a mensagem recebe o Auth Token).
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

_SUFIXOS_TWILIO = (".twilio.com", ".twiliocdn.com")


def midia_twilio_ok(url: str | None) -> bool:
    """True só para https em um domínio da Twilio (api.twilio.com, media.twiliocdn.com...)."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and p.port in (None, 443) and not p.username and \
        any(host.endswith(s) for s in _SUFIXOS_TWILIO)


def url_publica(request_url: str, headers: dict, caminho: str) -> str:
    """URL EXATA que a Twilio chamou. FINBOT_PUBLIC_URL (ex.: https://finbot.up.railway.app) tem prioridade;
    senão monta pelo proxy (X-Forwarded-Proto/Host), porque atrás do Railway o app enxerga http://."""
    base = (os.environ.get("FINBOT_PUBLIC_URL") or "").strip().rstrip("/")
    if base:
        return base + caminho
    h = {k.lower(): v for k, v in headers.items()}
    host = h.get("x-forwarded-host") or h.get("host")
    proto = (h.get("x-forwarded-proto") or "https").split(",")[0].strip()
    if host:
        return f"{proto}://{host.split(',')[0].strip()}{caminho}"
    return request_url


def assinatura_valida(url: str, params: dict, assinatura: str | None, auth_token: str) -> bool:
    if not assinatura or not auth_token:
        return False
    try:
        from twilio.request_validator import RequestValidator
        return bool(RequestValidator(auth_token).validate(url, params, assinatura))
    except Exception:
        return False


def validacao_ligada() -> bool:
    """Ligada por padrão. FINBOT_VALIDAR_TWILIO=0 desliga (só para emergência/depuração)."""
    return os.environ.get("FINBOT_VALIDAR_TWILIO", "1").strip() not in ("0", "false", "False", "nao", "não")
