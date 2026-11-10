"""Canal do painel: o chat do painel usa a MESMA lógica do WhatsApp, só que sem passar pela Twilio.

Quando o painel processa uma mensagem, ele abre um contexto (`usando(caixa)`). Dentro dele, tudo o que o app mandaria ao
WhatsApp (`main._send_whatsapp`, botões de confirmação, perguntas de revisão) é GUARDADO na caixa do usuário e o painel mostra na tela.
Fora do contexto nada muda: o WhatsApp continua enviando normalmente.

A caixa vive na memória do processo (1 instância; se o Railway reiniciar, o histórico do chat do painel recomeça). Cada usuário tem a sua,
chaveada pelo telefone REAL (o mesmo da sessão do painel).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

MAX_MENSAGENS = 300
LIMITE_POR_MINUTO = 20          # envios do usuário por minuto (protege o Gemini e o banco)


class Caixa:
    def __init__(self) -> None:
        self.msgs: deque = deque(maxlen=MAX_MENSAGENS)
        self.seq = 0
        self.ocupado = 0                              # quantas tarefas do usuário ainda estão rodando
        self.trava = asyncio.Lock()                   # uma por vez: a ordem importa (confirmações pendentes)
        self._envios: deque = deque()

    def add(self, de: str, texto: str = "", botoes: list[dict] | None = None, acao: str | None = None, tipo: str = "texto",
            dados: dict | None = None) -> dict:
        self.seq += 1
        m = {"id": self.seq, "de": de, "tipo": tipo, "texto": texto, "ts": int(time.time())}
        if dados:
            m["dados"] = dados
        if botoes:
            m["botoes"] = botoes
        if acao:
            m["acao"] = acao
        self.msgs.append(m)
        return m

    def depois_de(self, n: int) -> list[dict]:
        return [m for m in self.msgs if m["id"] > n]

    def pode_enviar(self, agora: float | None = None) -> bool:
        """Limite de envios por minuto. Chamar UMA vez por envio."""
        agora = agora or time.time()
        while self._envios and agora - self._envios[0] > 60:
            self._envios.popleft()
        if len(self._envios) >= LIMITE_POR_MINUTO:
            return False
        self._envios.append(agora)
        return True


_CAIXAS: dict[str, Caixa] = {}
_ATUAL: ContextVar[Caixa | None] = ContextVar("caixa_painel", default=None)


def caixa(telefone_real: str) -> Caixa:
    c = _CAIXAS.get(telefone_real)
    if c is None:
        c = _CAIXAS[telefone_real] = Caixa()
    return c


@contextmanager
def usando(c: Caixa):
    token = _ATUAL.set(c)
    try:
        yield c
    finally:
        _ATUAL.reset(token)


def ativo() -> bool:
    return _ATUAL.get() is not None


def emitir(texto: str, botoes: list[tuple[str, str]] | None = None, acao: str | None = None, tipo: str = "texto",
           dados: dict | None = None) -> None:
    """Resposta do bot para o chat do painel. `botoes` = [(id, título)] — o id é o mesmo payload que o WhatsApp mandaria."""
    c = _ATUAL.get()
    if c is None:
        return
    c.add("bot", texto, [{"id": i, "titulo": t} for i, t in (botoes or [])], acao, tipo=tipo, dados=dados)


def limpar() -> None:
    """Só para testes."""
    _CAIXAS.clear()
