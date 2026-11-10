"""Áudio do WhatsApp -> gasto, com confirmação em um toque.

Fluxo: o áudio é baixado da Twilio (main.py), o Gemini transcreve e extrai o gasto (extrair), o app mostra
"Entendi: R$ 45,00 · mercado · Alimentação › Supermercado" com botões e SÓ grava depois da confirmação
(confirmar -> tools.execute("registrar_gasto")). Se o áudio não for um gasto (pergunta, comando), a
transcrição segue para o agente como se tivesse sido digitada.

A confirmação pendente fica em memória (um gasto por usuário, vale 15 min). Se o servidor reiniciar nesse
intervalo, o toque no botão responde "expirou, mande o áudio de novo" e nada é gravado.
Nada do áudio é guardado: os bytes só existem durante a chamada ao Gemini.
"""
import asyncio
import json
import logging
import re
import time
import unicodedata
from datetime import date, datetime

logger = logging.getLogger(__name__)

TTL_SEGUNDOS = 15 * 60
MAX_BYTES = 3 * 1024 * 1024          # ~ 1-2 min de áudio do WhatsApp; acima disso pede para digitar
MODELOS = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]
VALOR_MAXIMO = 1_000_000.0

_PENDENTES: dict[str, dict] = {}     # user_phone -> {"args": {...}, "forma_definida": bool, "ts": float}
_client = None


class AudioIlegivel(Exception):
    """O Gemini não devolveu uma transcrição utilizável."""


_PROMPT = """Você recebe um áudio de WhatsApp, em português do Brasil, de uma pessoa usando um app de finanças pessoais.
Hoje é {hoje}.

Transcreva fielmente o que a pessoa disse e devolva APENAS um JSON com estas chaves:
- "transcricao": o texto do áudio.
- "tipo": "gasto" somente se a pessoa conta uma despesa JÁ FEITA e diz o valor (ex.: "gastei 45 no mercado", "paguei 120 de luz").
  Qualquer outra coisa (pergunta, comando, saudação, receita, conversa) é "outro".
- "valor": número em reais (ex.: 45.5). "quarenta e cinco e cinquenta" = 45.5. Se o valor não ficou claro ou há dúvida, null.
- "descricao": nome curto do estabelecimento ou do item (ex.: "mercado", "uber", "farmácia"). Null se não disse.
- "forma_pagamento": "credito" se falou cartão de crédito ou parcelado; "debito" se falou débito, pix ou boleto;
  "dinheiro" se falou dinheiro ou espécie; null se não disse.
- "parcelas": número inteiro se falou "em 3x" ou "3 vezes"; senão null.
- "data": AAAA-MM-DD se disse quando foi (ontem, anteontem, dia 5); null se não disse (vale hoje).

Não invente nada que não foi dito. Responda APENAS com o JSON."""


def _cliente():
    global _client
    if _client is None:
        from google import genai
        from app.config import get_settings
        _client = genai.Client(api_key=get_settings().gemini_api_key)
    return _client


async def _gerar_gemini(audio: bytes, mime: str, prompt: str) -> str:
    from google.genai import types
    contents = [types.Content(role="user", parts=[types.Part.from_bytes(data=audio, mime_type=mime),
                                                  types.Part.from_text(text=prompt)])]
    ultimo = None
    for modelo in MODELOS:
        for tentativa in range(2):
            try:
                r = await _cliente().aio.models.generate_content(
                    model=modelo, contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0,
                                                       max_output_tokens=1024))
                if r.text and r.text.strip():
                    return r.text.strip()
            except Exception as exc:
                ultimo = exc
                if tentativa == 0 and any(c in str(exc) for c in ("429", "503", "exhausted")):
                    await asyncio.sleep(2)      # o usuário está esperando: espera curta
                    continue
                logger.warning(f"Áudio: modelo {modelo} falhou ({type(exc).__name__}).")
                break
    raise AudioIlegivel(f"sem resposta do Gemini: {type(ultimo).__name__ if ultimo else 'vazia'}")


def _tipo_mime(content_type: str | None) -> str:
    mime = (content_type or "audio/ogg").split(";")[0].strip().lower()
    return mime if mime.startswith("audio/") else "audio/ogg"


def _numero(v) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return round(n, 2) if 0 < n <= VALOR_MAXIMO else None


def interpretar_resposta_gemini(texto: str) -> dict:
    """Valida o JSON do Gemini; devolve sempre as mesmas chaves, com valor inválido virando None."""
    limpo = re.sub(r"```json\s?|\s?```", "", texto or "").strip()
    try:
        d = json.loads(limpo)
    except ValueError as exc:
        raise AudioIlegivel("resposta não é JSON") from exc
    if not isinstance(d, dict):
        raise AudioIlegivel("resposta não é um objeto")
    transcricao = str(d.get("transcricao") or "").strip()
    if not transcricao:
        raise AudioIlegivel("sem transcrição")
    forma = _sem_acento(str(d.get("forma_pagamento") or ""))   # "Crédito"/"crédito" -> "credito"
    data = None
    if d.get("data"):
        try:
            data = datetime.strptime(str(d["data"]), "%Y-%m-%d").date().isoformat()
        except ValueError:
            data = None
    try:
        parcelas = int(d.get("parcelas")) if d.get("parcelas") else None
    except (TypeError, ValueError):
        parcelas = None
    if parcelas is not None and not 2 <= parcelas <= 48:
        parcelas = None
    return {
        "transcricao": transcricao,
        "tipo": "gasto" if str(d.get("tipo") or "").lower() == "gasto" else "outro",
        "valor": _numero(d.get("valor")),
        "descricao": str(d.get("descricao") or "").strip()[:80] or None,
        "forma_pagamento": forma if forma in ("credito", "debito", "dinheiro") else None,
        "parcelas": parcelas,
        "data": data,
    }


async def extrair(audio: bytes, content_type: str | None, hoje: date | None = None, gerar=None) -> dict:
    hoje = hoje or date.today()
    gerar = gerar or _gerar_gemini
    bruto = await gerar(audio, _tipo_mime(content_type), _PROMPT.format(hoje=hoje.isoformat()))
    return interpretar_resposta_gemini(bruto)


def eh_gasto(dados: dict) -> bool:
    return dados.get("tipo") == "gasto" and bool(dados.get("valor"))


# ── confirmação pendente ─────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def preparar(user_phone: str, dados: dict, categorizar=None) -> dict:
    """Monta os argumentos de registrar_gasto (ainda NÃO grava) e guarda como pendente."""
    if categorizar is None:
        from app.categorizer import categorizar_gasto_hibrido

        async def categorizar(u, d):
            return await categorizar_gasto_hibrido(u, d, permitir_melhor_esforco=True)
    descricao = dados.get("descricao") or "Gasto por áudio"
    try:
        cat, sub = await categorizar(user_phone, descricao)
    except Exception as exc:
        logger.warning(f"Áudio: categorização falhou ({type(exc).__name__}); vai para A classificar.")
        cat, sub = "Outros", "A classificar"
    args = {"valor": dados["valor"], "categoria": cat, "subcategoria": sub, "descricao": descricao,
            "payment_method": dados.get("forma_pagamento") or "debito"}
    if dados.get("data"):
        args["data"] = dados["data"]
    if dados.get("parcelas") and args["payment_method"] == "credito":
        args["parcelas"] = dados["parcelas"]
    pend = {"args": args, "forma_definida": bool(dados.get("forma_pagamento")), "ts": time.time()}
    _PENDENTES[user_phone] = pend
    return pend


def obter(user_phone: str, agora: float | None = None) -> dict | None:
    pend = _PENDENTES.get(user_phone)
    if pend and (agora or time.time()) - pend["ts"] > TTL_SEGUNDOS:
        _PENDENTES.pop(user_phone, None)
        return None
    return pend


def tem_pendente(user_phone: str) -> bool:
    return obter(user_phone) is not None


def limpar(user_phone: str) -> None:
    _PENDENTES.pop(user_phone, None)


_FORMAS = {"debito": "débito ou Pix", "credito": "cartão de crédito", "dinheiro": "dinheiro"}


def texto_confirmacao(pend: dict) -> tuple[str, list[tuple[str, str]]]:
    """(texto numa linha só, botões). Sem a forma de pagamento dita, os botões já perguntam a forma."""
    a = pend["args"]
    quando = f" · {datetime.strptime(a['data'], '%Y-%m-%d').strftime('%d/%m')}" if a.get("data") else ""
    parc = f" em {a['parcelas']}x" if a.get("parcelas") else ""
    cat = a["categoria"] + (f" › {a['subcategoria']}" if a.get("subcategoria") else "")
    base = f"Entendi: R$ {_fmt(a['valor'])}{parc} · {a['descricao']} · {cat}{quando}"
    if pend["forma_definida"]:
        return f"{base} · {_FORMAS[a['payment_method']]}. Registro?", [("g:ok", "✅ Registrar"), ("g:no", "❌ Cancelar")]
    return f"{base}. Foi no débito/Pix ou no crédito?", [("g:d", "Débito ou Pix"), ("g:c", "Cartão de crédito"), ("g:no", "❌ Cancelar")]


def texto_confirmacao_sem_botoes(pend: dict) -> str:
    linha, _ = texto_confirmacao(pend)
    if pend["forma_definida"]:
        return f"🎙️ {linha}\n\nResponda *sim* para registrar ou *não* para cancelar."
    return f"🎙️ {linha}\n\nResponda *débito*, *crédito* ou *não* para cancelar."


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn").lower().strip(" .!?")


_TEXTO_PARA_ACAO = {
    "sim": "ok", "s": "ok", "ok": "ok", "confirmo": "ok", "confirmar": "ok", "registrar": "ok", "isso": "ok", "certo": "ok",
    "nao": "no", "n": "no", "cancelar": "no", "cancela": "no",
    "debito": "d", "pix": "d", "boleto": "d",
    "credito": "c", "cartao": "c", "cartao de credito": "c",
    "dinheiro": "m",
}


def interpretar_texto(texto: str) -> str | None:
    """Resposta digitada à confirmação ('sim', 'não', 'crédito'...) -> ação; qualquer outra coisa -> None."""
    return _TEXTO_PARA_ACAO.get(_sem_acento(texto))


def acao_do_botao(payload: str) -> str | None:
    return {"g:ok": "ok", "g:no": "no", "g:d": "d", "g:c": "c"}.get(str(payload or ""))


_FORMA_DA_ACAO = {"d": "debito", "c": "credito", "m": "dinheiro"}


def _resumo_gravado(res: dict, args: dict) -> str:
    cat = args["categoria"] + (f" › {args['subcategoria']}" if args.get("subcategoria") else "")
    linha = f"✅ Registrado: R$ {_fmt(args['valor'])} · {args['descricao']} · {cat}"
    if res.get("tipo") == "parcelado":
        linha += f" · {args.get('parcelas')}x"
    elif res.get("fatura_label"):
        linha += f" · fatura {res['fatura_label']}"
    if args.get("categoria") == "Outros" and args.get("subcategoria") == "A classificar":
        linha += "\n🔎 Ficou em *A classificar*: digite *revisar* quando quiser escolher a categoria."
    return linha


async def confirmar(user_phone: str, acao: str, executar=None) -> str:
    """Aplica o toque do usuário. Devolve a mensagem de resposta. Só aqui o gasto é gravado."""
    pend = obter(user_phone)
    if not pend:
        return "Essa confirmação expirou 😉 Mande o áudio de novo (ou digite o gasto)."
    if acao == "no":
        limpar(user_phone)
        return "Cancelado. Nada foi registrado."
    args = dict(pend["args"])
    if acao in _FORMA_DA_ACAO:
        args["payment_method"] = _FORMA_DA_ACAO[acao]
        if args["payment_method"] != "credito":
            args.pop("parcelas", None)
    if executar is None:
        from app import tools
        executar = tools.execute
    limpar(user_phone)               # um toque = uma gravação (toque repetido cai em "expirou")
    try:
        res = await executar("registrar_gasto", args, user_phone)
    except Exception as exc:
        logger.error(f"Áudio: falha ao gravar o gasto ({type(exc).__name__}: {exc})")
        return "⚠️ Não consegui registrar agora. Pode mandar o áudio de novo?"
    if not res.get("registrado"):
        return f"⚠️ Não consegui registrar: {res.get('erro') or 'erro desconhecido'}."
    return _resumo_gravado(res, args)
