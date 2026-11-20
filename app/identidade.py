# app/identidade.py
# Apelido de telefone (conta em conjunto): o número de uma segunda pessoa passa a usar os DADOS do titular,
# mas as respostas continuam indo para o celular de quem escreveu.
#
# Configuração (variável de ambiente, sem SQL):
#   FINFLOW_ALIAS=whatsapp:+5511999990000=whatsapp:+5511888880000
#   (apelido=titular; vários pares separados por vírgula ou ponto e vírgula; o "whatsapp:" e o "+" são opcionais)
# Sem a variável, tudo se comporta como antes. Para desfazer: apague a variável.
import contextvars
import os
import re

import app.demo as demo

# Número REAL de quem está sendo atendido agora (definido no início de cada mensagem; cada requisição tem o seu contexto).
_ORIGEM: contextvars.ContextVar[str | None] = contextvars.ContextVar("finflow_origem", default=None)


def _norm(tel: str) -> str:
    """'+55 (11) 99999-0000' / 'whatsapp:+5511999990000' -> 'whatsapp:+5511999990000'. Vazio se não houver dígitos."""
    d = re.sub(r"\D", "", tel or "")
    return f"whatsapp:+{d}" if d else ""


def _mapa() -> dict[str, str]:
    """{apelido normalizado: titular normalizado}. Ignora pares inválidos, auto-referência e cadeias (titular que também é apelido)."""
    bruto = os.environ.get("FINFLOW_ALIAS", "") or ""
    pares = {}
    for item in re.split(r"[;,]", bruto):
        if "=" not in item:
            continue
        a, t = (_norm(x) for x in item.split("=", 1))
        if a and t and a != t:
            pares[a] = t
    return {a: t for a, t in pares.items() if t not in pares}


def titular_de(telefone: str) -> str | None:
    """Titular dos dados deste número, se ele for um apelido; senão None."""
    return _mapa().get(_norm(telefone))


def efetivo(real: str) -> str:
    """Telefone a usar nos dados: o sintético se o modo demo está ligado; senão o titular (se for apelido); senão o próprio."""
    if demo.ativo(real):
        return demo.telefone_demo(real)
    return titular_de(real) or real


def definir_origem(real: str | None) -> None:
    _ORIGEM.set(real)


def destino(to: str) -> str:
    """Número para o qual a Twilio deve enviar. Demo: o número real. Dados do titular acessados por um apelido: o apelido
    (quem escreveu). Qualquer outro caso (alertas, cron, o próprio titular): o número recebido."""
    if demo.eh_telefone_demo(to):
        return demo.telefone_real(to)
    origem = _ORIGEM.get()
    if origem and titular_de(origem) == _norm(to):
        return origem
    return to
