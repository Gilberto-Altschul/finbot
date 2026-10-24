"""Nome do titular: serve para reconhecer Pix/TED entre as contas do PRÓPRIO usuário (movimento = transferência).

Três caminhos, nesta ordem:
  1. Automático: a Pluggy informa `owner` (e `taxNumber`) em cada conta. Só entra se for PESSOA FÍSICA —
     o nome de uma empresa nunca vira "nome do titular" (o pró-labore pago pela sua empresa é receita, não transferência).
  2. Comando no WhatsApp: "meu nome é Fulano da Silva" (ou "meus nomes" para ver o que está cadastrado).
  3. Dica: depois de "sincronizar", se nenhum nome estiver cadastrado, o bot lembra do comando.

A regra que USA o nome mora no banco (finbot_movimento, migration 007). Aqui só se grava o nome e se pede
a reclassificação do histórico do usuário (finbot_reclassificar_movimento, migration 008).
"""
from __future__ import annotations

import logging
import re

import app.database as db
from app.utils import _normalize, get_lookup_prefix

logger = logging.getLogger(__name__)

T_ALIASES = "finbot_user_aliases"
_EMPRESA = re.compile(r"\b(ltda|s\.?a\.?|eireli|mei|epp|me|ss|cia|comercio|servicos|sistemas|tecnologia|"
                      r"holding|associacao|fundacao|instituto|banco|bank)\b")
_CMD_NOME = re.compile(r"^\s*meu\s+nome(?:\s+completo)?\s+(?:é|e)\s+(.+?)\s*[.!]?\s*$", re.IGNORECASE)
_CMD_NOMES = {"meus nomes", "meu nome", "nomes cadastrados", "meu nome completo"}


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def eh_pessoa_fisica(owner, tax_number=None) -> bool:
    """CPF (11 dígitos) = pessoa física. CNPJ (14) = empresa. Sem documento, desconfia de nome com cara de empresa."""
    nome = " ".join(str(owner or "").split())
    if len(nome.split()) < 2:
        return False
    d = _so_digitos(tax_number)
    if len(d) == 14:
        return False
    if len(d) == 11:
        return True
    return not _EMPRESA.search(_normalize(nome))


def nome_valido(nome: str) -> bool:
    nome = " ".join(str(nome or "").split())
    if not (4 <= len(nome) <= 80) or len(nome.split()) < 2 or len(nome.split()) > 8:
        return False
    return all(ch.isalpha() or ch in " '’-." for ch in nome) and not _EMPRESA.search(_normalize(nome))


def nomes_cadastrados(user_phone: str) -> list[str]:
    res = db.get_db().table(T_ALIASES).select("nome").eq("user_key", get_lookup_prefix(user_phone)).execute()
    return sorted({r["nome"] for r in (res.data or []) if r.get("nome")})


def tem_nome(user_phone: str) -> bool:
    try:
        return bool(nomes_cadastrados(user_phone))
    except Exception as e:
        logger.warning(f"Não consegui ler os nomes do titular ({e}).")
        return True        # na dúvida, não incomoda o usuário com a dica


def _gravar(user_phone: str, nomes: list[str]) -> list[str]:
    """Grava os nomes que ainda não existem (comparando sem acento/caixa). Devolve os novos."""
    ja = {_normalize(n) for n in nomes_cadastrados(user_phone)}
    novos = []
    for n in nomes:
        n = " ".join(str(n).split())
        if n and _normalize(n) not in ja:
            novos.append(n); ja.add(_normalize(n))
    if novos:
        db.get_db().table(T_ALIASES).upsert(
            [{"user_key": get_lookup_prefix(user_phone), "nome": n} for n in novos], on_conflict="user_key,nome").execute()
    return novos


def reclassificar(user_phone: str) -> int:
    """Recalcula o movimento do histórico do usuário (usa a função do banco). Devolve quantas linhas mudaram."""
    try:
        res = db.get_db().rpc("finbot_reclassificar_movimento", {"p_user_key": get_lookup_prefix(user_phone)}).execute()
        dado = res.data
        if isinstance(dado, list):
            dado = dado[0] if dado else 0
        return int(dado or 0)
    except Exception as e:
        logger.warning(f"Não consegui reclassificar o histórico ({e}).")
        return 0


def registrar(user_phone: str, contas: list[dict]) -> list[str]:
    """Chamado na sincronização: grava o titular informado pela Pluggy (só pessoa física). Nunca levanta erro."""
    try:
        candidatos = [c.get("titular") for c in contas if eh_pessoa_fisica(c.get("titular"), c.get("documento"))]
        novos = _gravar(user_phone, candidatos)
        if novos:
            n = reclassificar(user_phone)
            logger.info(f"Titular cadastrado automaticamente ({len(novos)} nome(s)); {n} lançamento(s) reclassificado(s).")
        return novos
    except Exception as e:
        logger.warning(f"Não consegui registrar o titular ({e}).")
        return []


# ── comando de WhatsApp ──────────────────────────────────────────────────────
def eh_comando(texto: str) -> bool:
    t = (texto or "").strip()
    return bool(_CMD_NOME.match(t)) or t.lower() in _CMD_NOMES


def responder(user_phone: str, texto: str) -> str:
    t = (texto or "").strip()
    m = _CMD_NOME.match(t)
    if not m:
        nomes = nomes_cadastrados(user_phone)
        if not nomes:
            return ("Ainda não tenho o seu nome. Digite assim: *meu nome é Maria da Silva Souza* "
                    "(o nome completo, como aparece nos seus Pix).")
        return "Seus nomes cadastrados:\n" + "\n".join(f"• {n}" for n in nomes) + \
               "\n\nPara acrescentar outro: *meu nome é Nome Completo*."
    nome = " ".join(m.group(1).split())
    if not nome_valido(nome):
        return ("Não consegui usar esse nome 🤔 Preciso do nome completo de pessoa (nome e sobrenome, sem números). "
                "Ex.: *meu nome é Maria da Silva Souza*.")
    novos = _gravar(user_phone, [nome])
    if not novos:
        return f"Já tenho *{nome}* cadastrado 👍"
    n = reclassificar(user_phone)
    extra = (f"\n\n🔁 Revisei seu histórico: {n} lançamento(s) mudaram de tipo (transferência entre contas suas "
             "deixa de contar como gasto ou receita).") if n else ""
    return (f"✅ Anotado: *{nome}*. Pix e TED com esse nome passam a ser tratados como transferência entre as suas "
            f"contas, e não entram nos totais de gasto nem de receita.{extra}")


DICA = ("\n\n💡 Para eu não contar Pix entre as suas próprias contas como gasto ou receita, me diga seu nome completo: "
        "*meu nome é Fulano da Silva*.")


def dica(user_phone: str) -> str:
    """Texto para anexar à resposta de 'sincronizar' quando ainda não há nome cadastrado."""
    return "" if tem_nome(user_phone) else DICA


def anexar_dica(user_phone: str, mensagem: str) -> str:
    """Põe a dica no fim da resposta, mas ANTES da pergunta de revisão: o envio por botões (main._send_whatsapp)
    só funciona quando a mensagem TERMINA com o texto da pergunta."""
    d = dica(user_phone)
    if not d:
        return mensagem
    try:
        from app import revisao
        est = revisao.ler_estado(user_phone)
        texto = (est or {}).get("texto")
        if texto and mensagem.endswith(texto):
            return mensagem[: -len(texto)].rstrip() + d + "\n\n" + texto
    except Exception as e:
        logger.warning(f"Não consegui posicionar a dica antes da pergunta ({e}).")
    return mensagem + d
