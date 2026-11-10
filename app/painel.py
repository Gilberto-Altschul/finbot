"""Painel web do FinFlow (esqueleto): lê os mesmos dados do WhatsApp, em tela.

Entrada: o comando `painel` no WhatsApp gera um LINK DE USO ÚNICO (vale 10 min). Abrir o link troca o código por uma
sessão (cookie HttpOnly, 12 h). Nada de senha: quem recebe o link é quem tem o WhatsApp.

Só funciona com FINBOT_PUBLIC_URL definida (é ela que monta o link). Sem ela, o comando avisa e as rotas respondem 503.
Códigos e sessões vivem na memória do processo (1 instância): se o app reiniciar, é só pedir `painel` de novo.
Todas as rotas de dados exigem a sessão e usam o mesmo telefone do WhatsApp (ou o usuário do modo demo).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import date

from fastapi import APIRouter, Cookie, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response

import app.database as db
import app.demo as demo
import app.canal as canal
from app.utils import _normalize

logger = logging.getLogger(__name__)
router = APIRouter()

COOKIE = "finflow_sessao"
VALIDADE_CODIGO = 10 * 60          # segundos
VALIDADE_SESSAO = 12 * 3600
MAX_REGISTROS = 5000
_CODIGOS: dict[str, tuple[str, float]] = {}    # código -> (telefone real, expira)
_SESSOES: dict[str, tuple[str, float]] = {}    # sessão -> (telefone real, expira)


def base_url() -> str:
    return (os.environ.get("FINBOT_PUBLIC_URL") or "").strip().rstrip("/")


def ligado() -> bool:
    return bool(base_url())


def _limpar(tab: dict) -> None:
    agora = time.time()
    for k in [k for k, (_p, exp) in tab.items() if exp <= agora]:
        tab.pop(k, None)
    while len(tab) > MAX_REGISTROS:
        tab.pop(next(iter(tab)))


def link_whatsapp() -> str:
    """https://wa.me/<número do FinBot> (o mesmo número de onde o bot responde). Vazio se não der para saber."""
    try:
        from app.config import get_settings
        numero = "".join(ch for ch in str(get_settings().twilio_whatsapp_number or "") if ch.isdigit())
    except Exception:
        return ""
    return f"https://wa.me/{numero}" if numero else ""


def _com_whatsapp(pagina: str) -> str:
    """Troca o marcador do link do WhatsApp; sem número, o botão some."""
    link = link_whatsapp()
    if link:
        return pagina.replace("__WA__", html.escape(link, quote=True))
    return pagina.replace("<a href=\"__WA__\"", "<a hidden href=\"#\"")


def eh_comando(texto: str | None) -> bool:
    return _normalize(texto or "").strip(" .!?") in ("painel", "abrir painel", "meu painel", "painel web", "finflow")


def criar_codigo(telefone: str) -> str:
    """Código novo para este telefone; os anteriores dele deixam de valer."""
    _limpar(_CODIGOS)
    for k in [k for k, (p, _e) in _CODIGOS.items() if p == telefone]:
        _CODIGOS.pop(k, None)
    codigo = secrets.token_urlsafe(24)
    _CODIGOS[codigo] = (telefone, time.time() + VALIDADE_CODIGO)
    return codigo


def consumir_codigo(codigo: str | None) -> str | None:
    """Telefone do código (e o invalida), ou None se não existe, venceu ou já foi usado."""
    item = _CODIGOS.pop(codigo or "", None)
    if not item or item[1] <= time.time():
        return None
    return item[0]


def codigo_valido(codigo: str | None) -> bool:
    """O código existe e não venceu? Só olha: NÃO gasta (pré-visualizadores e verificadores de link abrem o GET)."""
    item = _CODIGOS.get(codigo or "")
    return bool(item and item[1] > time.time())


def criar_sessao(telefone: str) -> str:
    _limpar(_SESSOES)
    sid = secrets.token_urlsafe(32)
    _SESSOES[sid] = (telefone, time.time() + VALIDADE_SESSAO)
    return sid


# Sessão PERSISTENTE (padrão 30 dias): o cookie leva o telefone cifrado e assinado (Fernet com chave derivada da chave do app), então
# sobrevive a reinício do Railway e o painel instalado não pede link novo todo dia. Desligar/encurtar: FINFLOW_SESSAO_DIAS=0 (volta às
# sessões de 12 h em memória). "Sair" invalida o cookie na hora (lista em memória) e o apaga do navegador.
_PREFIXO_PERSISTENTE = "p1."
_REVOGADAS: set[str] = set()


def dias_sessao() -> int:
    try:
        return max(0, min(90, int(os.environ.get("FINFLOW_SESSAO_DIAS", "30"))))
    except ValueError:
        return 30


def _cifra_sessao():
    """Fernet exclusivo das sessões do painel (derivado da chave do app, para o token não servir em outro lugar). None sem chave."""
    try:
        import base64, hashlib
        from cryptography.fernet import Fernet
        from app.config import get_settings
        chave = str(get_settings().encryption_key or "")
        if not chave:
            return None
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"finflow-sessao|" + chave.encode()).digest()))
    except Exception:
        return None


def validade_cookie() -> int:
    return dias_sessao() * 86400 if (dias_sessao() > 0 and _cifra_sessao()) else VALIDADE_SESSAO


def criar_cookie_sessao(telefone: str) -> str:
    """Valor do cookie ao abrir o link: persistente se possível, senão a sessão de 12 h em memória."""
    cifra = _cifra_sessao()
    if dias_sessao() > 0 and cifra:
        return _PREFIXO_PERSISTENTE + cifra.encrypt(telefone.encode()).decode().rstrip("=")      # sem "=": o cookie não precisa de aspas
    return criar_sessao(telefone)


def telefone_da_sessao(sid: str | None) -> str | None:
    if sid and sid.startswith(_PREFIXO_PERSISTENTE):
        cifra = _cifra_sessao()
        if not cifra or dias_sessao() <= 0 or sid in _REVOGADAS:
            return None
        try:
            corpo = sid[len(_PREFIXO_PERSISTENTE):]
            return cifra.decrypt((corpo + "=" * (-len(corpo) % 4)).encode(), ttl=dias_sessao() * 86400).decode()
        except Exception:
            return None
    item = _SESSOES.get(sid or "")
    if not item:
        return None
    if item[1] <= time.time():
        _SESSOES.pop(sid, None)
        return None
    return item[0]


def mensagem_link(telefone: str) -> str:
    """Resposta do comando `painel` no WhatsApp."""
    if not ligado():
        return "O painel ainda não está disponível. 🙏"
    link = f"{base_url()}/painel/entrar?c={criar_codigo(telefone)}"
    return (f"🖥️ *Seu painel FinFlow*\n{link}\n\n"
            "O link vale por 10 minutos e abre uma única vez. Para voltar depois, é só mandar *painel* de novo.")


# ── dados ────────────────────────────────────────────────────────────────────
def dados_resumo(user_phone: str, hoje: date | None = None) -> dict:
    """Mesmos números do `resumo` do WhatsApp, como dados: receitas, gastos, saldo e categorias com orçamento."""
    hoje = hoje or date.today()
    mes = hoje.strftime("%Y-%m")
    gastos = db.monthly_total(user_phone, mes)
    receitas = db.monthly_income_total(user_phone, mes)
    orc = {_normalize(b["category"]): float(b["amount"]) for b in db.get_all_budgets(user_phone, mes)}
    # Previstas (parcelas que o banco ainda não postou) contam como lançadas, como no Resumo do WhatsApp.
    prev_total, prev_cat = 0.0, {}
    try:
        from app import dados_banco as dbk
        prev_total, _n, prev_cat = dbk.parcelas_previstas_do_mes(user_phone, date(hoje.year, hoje.month, 1))
    except Exception:
        pass
    por_cat = {}
    for r in db.monthly_by_category(user_phone, mes):
        por_cat[r["category"]] = float(r["total"])
    for c, v in prev_cat.items():
        por_cat[c] = round(por_cat.get(c, 0.0) + v, 2)
    gastos_realizados, gastos = gastos, round(gastos + prev_total, 2)
    cats = []
    for r in sorted(({"category": c, "total": v} for c, v in por_cat.items()), key=lambda x: -x["total"]):
        limite = orc.get(_normalize(r["category"]))
        pct = round(r["total"] / limite * 100) if limite else None
        status = None if pct is None else ("estourado" if pct > 100 else ("atencao" if pct >= 80 else "saudavel"))
        cats.append({"nome": r["category"], "gasto": r["total"], "orcamento": limite, "pct": pct, "status": status})
    return {"mes": mes, "receitas": receitas, "gastos": gastos, "saldo": round(receitas - gastos, 2),
            "previstas": prev_total, "gastos_realizados": gastos_realizados,
            "demonstracao": demo.eh_telefone_demo(user_phone), "categorias": cats}


def dados_categoria(user_phone: str, categoria: str, hoje: date | None = None) -> dict:
    """Detalhe de UMA categoria no mês: subcategorias (do maior para o menor) e os lançamentos de cada uma.
    Os totais batem com o Resumo: lançamentos do mês + parcelas previstas que o banco ainda não postou."""
    hoje = hoje or date.today()
    mes = hoje.strftime("%Y-%m")
    alvo = _normalize(categoria)
    linhas = db.expenses_of_category_month(user_phone, categoria, mes)
    previstas = 0.0
    try:
        from app import dados_banco as dbk
        _t, _n, prev_cat = dbk.parcelas_previstas_do_mes(user_phone, date(hoje.year, hoje.month, 1))
        previstas = round(sum(v for c, v in prev_cat.items() if _normalize(c) == alvo), 2)
    except Exception:
        pass
    grupos: dict[str, dict] = {}
    for r in linhas:
        sub = (r.get("subcategory") or "").strip() or "Sem subcategoria"
        g = grupos.setdefault(_normalize(sub), {"nome": sub, "total": 0.0, "itens": []})
        valor = round(float(r.get("amount") or 0), 2)
        g["total"] += valor
        parcela = (f"{r['installment_of']}/{r['installment_total']}"
                   if r.get("installment_of") and r.get("installment_total") else None)
        g["itens"].append({"descricao": (r.get("description") or "Sem descrição").strip(), "valor": valor,
                           "data": (r.get("purchase_date") or r.get("billing_date") or "")[:10],
                           "forma": (r.get("payment_method") or "").lower(), "parcela": parcela,
                           "revisar": r.get("review_status") == "revisar"})
    subs = sorted(grupos.values(), key=lambda g: -g["total"])
    for g in subs:
        g["total"] = round(g["total"], 2)
        g["n"] = len(g["itens"])
        g["itens"].sort(key=lambda i: (i["data"], i["valor"]), reverse=True)
    total = round(sum(g["total"] for g in subs) + previstas, 2)
    orc = next((float(b["amount"]) for b in db.get_all_budgets(user_phone, mes) if _normalize(b["category"]) == alvo), None)
    pct = round(total / orc * 100) if orc else None
    status = None if pct is None else ("estourado" if pct > 100 else ("atencao" if pct >= 80 else "saudavel"))
    nome = next((r["category"].strip() for r in linhas if r.get("category")), categoria)
    # Mês anterior (só o que foi lançado; parcelas previstas não existem mais para um mês fechado)
    ini_ant = date(hoje.year - (hoje.month == 1), 12 if hoje.month == 1 else hoje.month - 1, 1)
    anterior = round(sum(float(r.get("amount") or 0) for r in
                         db.expenses_of_category_month(user_phone, categoria, ini_ant.strftime("%Y-%m"))), 2)
    return {"mes": mes, "categoria": nome, "total": total, "orcamento": orc, "pct": pct, "status": status,
            "previstas": previstas, "lancamentos": len(linhas), "subcategorias": subs,
            "a_revisar": sum(1 for r in linhas if r.get("review_status") == "revisar"),
            "mes_anterior": ini_ant.strftime("%Y-%m"), "anterior": anterior,
            "dif": round(total - anterior, 2) if anterior else None}


def _usuario(sessao: str | None) -> tuple[str, str]:
    """(telefone real, telefone dos dados). 401 sem sessão válida."""
    real = telefone_da_sessao(sessao)
    if not real:
        raise HTTPException(status_code=401, detail="sessão expirada")
    return real, demo.efetivo(real)


def _json(dados: dict) -> JSONResponse:
    return JSONResponse(dados, headers={"Cache-Control": "no-store"})


# ── rotas ────────────────────────────────────────────────────────────────────
@router.get("/painel/entrar")
def entrar(request: Request, c: str = ""):
    """Só mostra o botão: o código NÃO é gasto aqui. Quem gasta é o POST (clique da pessoa), para que a
    pré-visualização do WhatsApp, verificadores de link e navegadores que carregam a página antes não queimem o link."""
    if not ligado():
        raise HTTPException(status_code=503, detail="painel indisponível")
    agente = (request.headers.get("user-agent") or "")[:80]
    if not codigo_valido(c):
        logger.info(f"Painel: link inválido, vencido ou já usado (agente: {agente}).")
        return HTMLResponse(_PAGINA_EXPIRADA, status_code=410, headers={"Cache-Control": "no-store"})
    logger.info(f"Painel: página de entrada aberta (agente: {agente}).")
    return HTMLResponse(_PAGINA_ENTRAR.replace("__CODIGO__", html.escape(c, quote=True)),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/painel/entrar")
def entrar_confirmar(c: str = Form(default="")):
    if not ligado():
        raise HTTPException(status_code=503, detail="painel indisponível")
    telefone = consumir_codigo(c)
    if not telefone:
        logger.info("Painel: clique em link inválido, vencido ou já usado.")
        return HTMLResponse(_PAGINA_EXPIRADA, status_code=410, headers={"Cache-Control": "no-store"})
    logger.info("Painel: sessão aberta.")
    resp = RedirectResponse("/painel", status_code=303)
    resp.set_cookie(COOKIE, criar_cookie_sessao(telefone), max_age=validade_cookie(), httponly=True, samesite="lax",
                    secure=base_url().startswith("https://"), path="/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/painel", response_class=HTMLResponse)
def pagina(finflow_sessao: str | None = Cookie(default=None)):
    if not ligado():
        raise HTTPException(status_code=503, detail="painel indisponível")
    if not telefone_da_sessao(finflow_sessao):
        logger.info("Painel: acesso sem sessão (cookie ausente, bloqueado ou sessão vencida).")
        return HTMLResponse(_com_whatsapp(_PAGINA_SEM_SESSAO), status_code=401, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_com_whatsapp(_PAGINA), headers={"Cache-Control": "no-store"})


@router.get("/painel/api/resumo")
def api_resumo(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    return _json(dados_resumo(usuario))


@router.get("/painel/api/categoria")
def api_categoria(nome: str = "", finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    nome = (nome or "").strip()[:80]
    if not nome:
        raise HTTPException(status_code=400, detail="categoria não informada")
    return _json(dados_categoria(usuario, nome))


@router.get("/painel/api/fluxo")
def api_fluxo(dias: int = 30, finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    import app.dados_banco as dados_banco       # import tardio: puxa o módulo de contas/faturas só quando usa
    import app.fluxo as fluxo
    return _json(fluxo.para_painel(dados_banco.fluxo_projecao(usuario, max(7, min(dias, 90))), dados_banco.hoje_local()))


@router.get("/painel/api/previsao")
def api_previsao(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    import app.dados_banco as dados_banco
    import app.previsao as previsao
    return _json(previsao.para_painel(*dados_banco.previsao_dados(usuario)))


class _Confirmacao(BaseModel):
    key: str
    categoria: str
    subcategoria: str


@router.get("/painel/api/revisar")
def api_revisar(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    import app.revisao as revisao
    return _json(revisao.lista_para_painel(usuario))


@router.get("/painel/api/categorias")
def api_categorias(finflow_sessao: str | None = Cookie(default=None)):
    """Categorias de despesa e suas subcategorias, para escolher 'outra categoria' na revisão."""
    _usuario(finflow_sessao)
    from app.taxonomia import CATEGORIAS_DESPESA, SUBCATEGORIAS, A_CLASSIFICAR
    cats = [{"nome": c, "subs": [x for x in SUBCATEGORIAS.get(c, []) if (c, x) != A_CLASSIFICAR]} for c in CATEGORIAS_DESPESA]
    return _json({"categorias": [c for c in cats if c["subs"]]})


@router.post("/painel/api/revisar/confirmar")
def api_revisar_confirmar(corpo: _Confirmacao, finflow_sessao: str | None = Cookie(default=None)):
    """Corpo JSON (não é um formulário simples: o navegador barra chamadas de outros sites) + cookie SameSite=Lax."""
    _real, usuario = _usuario(finflow_sessao)
    import app.revisao as revisao
    res = revisao.confirmar_para_painel(usuario, corpo.key[:200], corpo.categoria[:80], corpo.subcategoria[:80])
    logger.info(f"Painel: revisão {'confirmada' if res.get('ok') else 'recusada (' + str(res.get('motivo')) + ')'}.")
    return _json(res)


@router.get("/painel/saiu", response_class=HTMLResponse)
def saiu():
    """Tela de despedida (pública): sem sessão ela só informa, não é erro."""
    return HTMLResponse(_com_whatsapp(_PAGINA_SAIU), headers={"Cache-Control": "no-store"})


@router.post("/painel/sair")
def sair(request: Request, finflow_sessao: str | None = Cookie(default=None)):
    _SESSOES.pop(finflow_sessao or "", None)
    if finflow_sessao and finflow_sessao.startswith(_PREFIXO_PERSISTENTE):
        _REVOGADAS.add(finflow_sessao)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ── Chat no painel ───────────────────────────────────────────────────────────
# Mesma lógica do WhatsApp (comandos, gastos, perguntas, áudio, PDF), mas a resposta vai para a TELA e não para a Twilio.
# A lógica em si mora em main.py (registrar_chat); aqui ficam sessão, limites e a caixa de mensagens (app.canal).
_CHAT: dict = {}
_TAREFAS: set = set()
MAX_TEXTO = 1000
MAX_AUDIO = 3 * 1024 * 1024
MAX_PDF = 10 * 1024 * 1024
_PAYLOAD_OK = re.compile(r"^[rg]:[A-Za-z0-9:_\-]{1,60}$")


def registrar_chat(**fns) -> None:
    _CHAT.update(fns)


def _origem_ok(request: Request) -> bool:
    """Defesa extra além do SameSite: se o navegador informa a origem, ela tem que ser a do painel."""
    origem = request.headers.get("origin")
    return not origem or origem.rstrip("/") == base_url()


def _disparar(real: str, fn, *args) -> None:
    c = canal.caixa(real)
    c.ocupado += 1

    async def _rodar():
        try:
            await fn(*args)
        finally:
            c.ocupado -= 1
    t = asyncio.get_running_loop().create_task(_rodar())
    _TAREFAS.add(t)
    t.add_done_callback(_TAREFAS.discard)


def _checar_chat(request: Request, sessao: str | None) -> tuple[str, canal.Caixa]:
    if not ligado():
        raise HTTPException(status_code=503, detail="painel indisponível")
    real, _dados = _usuario(sessao)
    if not _CHAT:
        raise HTTPException(status_code=503, detail="chat indisponível")
    if request.method == "POST":
        if not _origem_ok(request):
            raise HTTPException(status_code=403, detail="origem não permitida")
        c = canal.caixa(real)
        if not c.pode_enviar():
            raise HTTPException(status_code=429, detail="muitas mensagens; espere um instante")
    return real, canal.caixa(real)


class _Mensagem(BaseModel):
    texto: str = ""
    payload: str | None = None


@router.get("/painel/api/chat/mensagens")
def api_chat_mensagens(request: Request, depois: int = 0, finflow_sessao: str | None = Cookie(default=None)):
    _real, c = _checar_chat(request, finflow_sessao)
    return _json({"mensagens": c.depois_de(max(0, depois)), "ocupado": c.ocupado > 0, "ultimo": c.seq})


@router.post("/painel/api/chat/enviar")
async def api_chat_enviar(corpo: _Mensagem, request: Request, finflow_sessao: str | None = Cookie(default=None)):
    real, c = _checar_chat(request, finflow_sessao)
    texto = (corpo.texto or "").strip()
    payload = (corpo.payload or "").strip() or None
    if len(texto) > MAX_TEXTO:
        raise HTTPException(status_code=413, detail="mensagem muito longa")
    if payload and not _PAYLOAD_OK.match(payload):
        raise HTTPException(status_code=400, detail="botão inválido")
    if not texto and not payload:
        raise HTTPException(status_code=400, detail="mensagem vazia")
    if texto:
        c.add("eu", texto)
    _disparar(real, _CHAT["texto"], real, texto, payload)
    return _json({"status": "ok", "ultimo": c.seq})


@router.post("/painel/api/chat/audio")
async def api_chat_audio(request: Request, audio: UploadFile = File(...), finflow_sessao: str | None = Cookie(default=None)):
    real, c = _checar_chat(request, finflow_sessao)
    tipo = (audio.content_type or "").lower()
    if not tipo.startswith("audio/"):
        raise HTTPException(status_code=415, detail="envie um áudio")
    dados = await audio.read(MAX_AUDIO + 1)
    if len(dados) > MAX_AUDIO:
        raise HTTPException(status_code=413, detail="áudio longo demais")
    if not dados:
        raise HTTPException(status_code=400, detail="áudio vazio")
    c.add("eu", "🎙️ Áudio", tipo="audio")
    _disparar(real, _CHAT["audio"], real, dados, tipo)
    return _json({"status": "ok", "ultimo": c.seq})


@router.post("/painel/api/chat/pdf")
async def api_chat_pdf(request: Request, arquivo: UploadFile = File(...), senha: str = Form(default=""),
                       finflow_sessao: str | None = Cookie(default=None)):
    real, c = _checar_chat(request, finflow_sessao)
    dados = await arquivo.read(MAX_PDF + 1)
    if len(dados) > MAX_PDF:
        raise HTTPException(status_code=413, detail="PDF grande demais (máximo 10 MB)")
    if not dados.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="envie um PDF")
    nome = (arquivo.filename or "extrato.pdf")[:60]
    c.add("eu", f"📎 {nome}", tipo="pdf")
    _disparar(real, _CHAT["pdf"], real, dados, (senha or "")[:40] or None)
    return _json({"status": "ok", "ultimo": c.seq})


# ── PWA: instalar o painel na tela inicial ──────────────────────────────────
# Tudo aqui é PÚBLICO e sem dado do usuário (manifesto, ícones, página offline). O service worker NÃO guarda páginas nem respostas
# da API (são privadas): ele só mostra uma tela "sem conexão" quando a rede cai ao abrir o painel.
PWA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "painel_pwa")
_ICONES = {"icone-192.png": "image/png", "icone-512.png": "image/png", "icone-maskable-512.png": "image/png",
           "apple-touch-icon.png": "image/png"}


@router.get("/painel/manifest.webmanifest")
def manifesto():
    dados = {"id": "/painel", "name": "FinFlow", "short_name": "FinFlow", "description": "Suas finanças do WhatsApp em tela.",
             "lang": "pt-BR", "start_url": "/painel", "scope": "/painel", "display": "standalone",
             "background_color": "#081226", "theme_color": "#081226",
             "icons": [{"src": "/painel/icone-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                       {"src": "/painel/icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                       {"src": "/painel/icone-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(dados, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@router.get("/painel/{arquivo}.png")
def icone(arquivo: str):
    nome = f"{arquivo}.png"
    if nome not in _ICONES:
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(PWA_DIR, nome), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


_SW = """// FinFlow: só mostra a tela "sem conexão" quando a rede cai ao ABRIR o painel. Nada privado fica guardado aqui.
const V = 'finflow-pwa-v2', OFFLINE = '/painel/offline';
self.addEventListener('install', e => { e.waitUntil(caches.open(V).then(c => c.addAll([OFFLINE, '/painel/icone-192.png'])).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  if (e.request.method === 'GET' && e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
  }
});
"""


@router.get("/painel/sw.js")
def service_worker():
    return Response(_SW, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/painel"})


@router.get("/painel/offline", response_class=HTMLResponse)
def offline():
    return HTMLResponse(_PAGINA_OFFLINE, headers={"Cache-Control": "no-cache"})


# ── páginas ──────────────────────────────────────────────────────────────────
_ESTILO = """<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600;700&display=swap"><style>body{margin:0;font-family:Inter,system-ui,sans-serif;background:#081226;color:#EAF0FF;color-scheme:dark;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
main{max-width:360px;display:flex;flex-direction:column;gap:12px}h1{font-family:"Bricolage Grotesque",Inter,sans-serif;font-size:26px;font-weight:800;letter-spacing:-.03em;margin:0}p{margin:0;line-height:1.5;color:#9AABD3}
button{min-height:52px;border:0;border-radius:16px;background:#2A62F5;color:#fff;font:inherit;font-size:16px;font-weight:700;cursor:pointer}</style>"""
_PAGINA_EXPIRADA = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Link vencido ou já usado</h1>"
    "<p>Mande <b>painel</b> no WhatsApp para receber um link novo. Cada link vale por 10 minutos e abre uma única vez.</p></main></body></html>")
_PAGINA_SEM_SESSAO = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Sessão não encontrada</h1>"
    "<p>Mande <b>painel</b> no WhatsApp para receber um link novo. Se você acabou de abrir um link e caiu aqui, abra o link no navegador do celular (Chrome ou Safari), "
    "não no navegador interno de outro aplicativo, e permita cookies.</p><a href=\"__WA__\" style=\"display:flex;align-items:center;justify-content:center;min-height:52px;border-radius:16px;background:#2A62F5;color:#fff;font-weight:700;text-decoration:none\">Abrir o WhatsApp</a></main></body></html>")
_PAGINA_SAIU = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Você saiu do painel</h1>"
    "<p>Seus dados continuam guardados. Para entrar de novo, mande <b>painel</b> no WhatsApp e abra o link novo.</p><a href=\"__WA__\" style=\"display:flex;align-items:center;justify-content:center;min-height:52px;border-radius:16px;background:#2A62F5;color:#fff;font-weight:700;text-decoration:none\">Abrir o WhatsApp</a></main></body></html>")
_PAGINA_OFFLINE = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Sem conexão</h1>"
    "<p>Não consegui abrir o painel agora. Confira a internet e tente de novo.</p>"
    "<button onclick=\"location.reload()\">Tentar de novo</button></main></body></html>")
_PAGINA_ENTRAR = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Abrir o seu painel</h1>"
    "<p id=\"msg\">Abrindo o painel… Se não abrir sozinho, toque no botão. O link abre uma única vez.</p>"
    "<form method=\"post\" action=\"/painel/entrar\"><input type=\"hidden\" name=\"c\" value=\"__CODIGO__\">"
    "<button type=\"submit\" style=\"width:100%\">Abrir meu painel</button></form></main>"
    # Entra sozinho quando um NAVEGADOR de verdade abre a página (sem toque extra). Pré-visualizadores e verificadores de link só leem o
    # HTML (não rodam script, ou se identificam como automação), então não gastam o link de uso único; o botão continua como alternativa.
    "<script>if(!navigator.webdriver){setTimeout(function(){var f=document.forms[0];if(f&&!window.__enviado){window.__enviado=1;f.submit()}},250)}</script>"
    "</body></html>")

_PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>FinFlow</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Inter:wght@400;500;600;700&display=swap"><style>
/* Identidade "Maré": azul profundo + Água (entradas), Coral (saídas), Farol (alertas). Display: Bricolage Grotesque; texto: Inter. */
:root{--v:#2A62F5;--vt:#8FB2FF;--ink:#EAF0FF;--mut:#9AABD3;--bg:#081226;--card:#0F1D3B;--lin:#1E3160;--corr:#12245A;--borda:#2A4486;--agua:#5FE3C0;--coral:#FF8A7A;--farol:#FFB45C;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
h1,h2,.saldo,.prev .grande{font-family:"Bricolage Grotesque",Inter,system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 104px;display:flex;flex-direction:column;gap:14px}
h1{margin:0;font-size:28px;font-weight:800;letter-spacing:-.03em}.mut{color:var(--mut);font-size:14px}a{color:var(--vt)}
.card{background:var(--card);border:1px solid var(--lin);border-radius:22px;padding:18px}
.hero{background:var(--corr);border:1px solid var(--borda);color:var(--ink);border-radius:26px;padding:20px;display:flex;flex-direction:column;gap:10px}
.lin{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.hero .r{color:#C9D6F5}.saldo{font-size:32px;font-weight:800;letter-spacing:-.03em}
.tag{display:inline-block;padding:5px 11px;border-radius:99px;background:#16295A;color:#C9D6F5;font-size:12px}
.cat{padding:14px 0;border-bottom:1px solid var(--lin);display:flex;flex-direction:column;gap:8px}.cat:last-child{border:0}
.barra{height:8px;border-radius:99px;background:var(--lin);overflow:hidden}.barra i{display:block;height:8px;border-radius:99px;background:var(--v)}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700}
.saudavel{background:#0F3A3A;color:var(--agua)}.atencao{background:#3A2A12;color:var(--farol)}.estourado{background:#3A1E26;color:var(--coral)}
#conteudo{display:flex;flex-direction:column;gap:14px}h2{margin:0;font-size:18px;font-weight:700;letter-spacing:-.02em}
.alerta{border-radius:22px;padding:16px;display:flex;flex-direction:column;gap:6px}.alerta.ruim{background:#3A1E26;color:#FFD2CB;border:1px solid #5A2A33}.alerta.bom{background:#0F3A3A;color:#BFF5E6;border:1px solid #1D5A55}
.alerta b{font-size:18px}.alerta span{font-size:14px;line-height:1.45}
.soma{display:flex;flex-direction:column}.soma .lin{padding:10px 0;border-bottom:1px solid var(--lin)}.soma .lin:last-child{border:0;font-weight:700;font-size:16px}
.neg{color:var(--coral)}.pos{color:var(--agua)}.dia{padding:14px 0;border-bottom:1px solid var(--lin);display:grid;grid-template-columns:52px 1fr;gap:4px 12px}.dia:last-child{border:0}
.dia .d{font-weight:700;font-size:15px}.dia .d small{display:block;font-weight:400;color:var(--mut);font-size:12px}
.ev{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:2px 0}.ev .n{display:flex;flex-direction:column;gap:2px;min-width:0}.ev .n span{font-size:15px;overflow-wrap:anywhere}.ev b{white-space:nowrap}.lin span:last-child{white-space:nowrap}
.tipo{font-size:12px;color:var(--mut)}.sd{font-size:13px;color:var(--mut);margin-top:4px}
details summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:700;color:var(--vt)}details p{margin:0;font-size:13px;line-height:1.5;color:var(--mut)}
h3{margin:0;font-size:15px}.prev{display:flex;flex-direction:column;gap:4px}.prev .grande{font-size:32px;font-weight:800;letter-spacing:-.03em}
.pb{position:relative;height:10px;border-radius:5px;background:var(--lin)}.pb i{position:absolute;top:0;bottom:0;display:block}.pb u{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.leg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut)}.leg span:before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px;background:var(--c)}
.cp{padding:14px 0;border-bottom:1px solid var(--lin);display:flex;flex-direction:column;gap:8px}.cp:last-child{border:0}
.ok{background:#0F3A3A;color:var(--agua)}.estoura{background:#3A1E26;color:var(--coral)}
button.op{min-height:44px;padding:0 14px;border-radius:14px;border:1px solid var(--borda);background:var(--corr);color:var(--ink);font:inherit;font-size:14px;font-weight:600;cursor:pointer;text-align:left}
button.op:disabled{opacity:.5}select{min-height:44px;border-radius:14px;border:1px solid var(--borda);background:var(--bg);font:inherit;font-size:15px;padding:0 10px;color:var(--ink);width:100%}
.rev,.cx{display:flex;flex-direction:column;gap:10px}.feito{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--agua);font-size:14px}.ops{display:flex;flex-wrap:wrap;gap:8px}
svg text{font:11px Inter,system-ui,sans-serif;fill:#9AABD3}
pre{white-space:pre-wrap;font:inherit;line-height:1.5;margin:0}
button.cat{width:100%;border:0;border-bottom:1px solid var(--lin);background:none;color:inherit;font:inherit;text-align:left;cursor:pointer;border-radius:0}
button.cat:last-child{border-bottom:0}button.cat:active{background:#132550}button.cat .seta{color:var(--mut);margin-left:6px}
button.cat:focus-visible,.voltar:focus-visible{outline:2px solid var(--vt);outline-offset:2px;border-radius:10px}
.voltar{align-self:flex-start;border:0;background:none;color:var(--vt);font:inherit;font-weight:700;min-height:44px;padding:0;cursor:pointer}
.sub{padding:12px 0;border-bottom:1px solid var(--lin);display:flex;flex-direction:column;gap:6px}.sub:last-child{border:0}
.it{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:10px 0;border-bottom:1px solid var(--lin)}.it:last-child{border:0}
.it .n{display:flex;flex-direction:column;gap:2px;min-width:0}.it .n span{font-size:15px;overflow-wrap:anywhere}.it b{white-space:nowrap}
details.gr{border-bottom:1px solid var(--lin)}details.gr:last-child{border:0}
details.gr>summary{list-style:none;min-height:52px;display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
details.gr>summary::-webkit-details-marker{display:none}details.gr>summary .q{display:flex;align-items:center;gap:8px}
details.gr>summary .q:after{content:"";width:8px;height:8px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(45deg);margin:-4px 2px 0}
details.gr[open]>summary .q:after{transform:rotate(-135deg);margin-top:4px}details.gr .it:last-child{border:0}
.avisorev{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#3A2A12;border:1px solid #5A4220;color:#FFE1BD;border-radius:18px;padding:12px 14px;font-size:14px}
.avisorev button{min-height:44px;padding:0 14px;border-radius:12px;border:0;background:var(--farol);color:#2A1A05;font:inherit;font-weight:700;cursor:pointer;white-space:nowrap}
.compara{font-size:13px;font-weight:600}
nav{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--lin);display:flex;gap:4px;padding:6px 8px 10px}
nav button{flex:1;min-height:56px;border:0;border-radius:16px;background:none;font:inherit;font-size:14px;font-weight:600;color:var(--mut);cursor:pointer}
nav button[aria-current=page]{color:var(--ink);background:var(--corr)}.sair{border:0;background:none;color:var(--vt);font:inherit;font-weight:700;min-height:44px;cursor:pointer}a.sair{background:none}
</style></head><body><main>
<div class="lin"><a href="__WA__" class="sair" style="display:flex;align-items:center;min-height:44px;text-decoration:none">← WhatsApp</a><button class="sair" id="sair" type="button">Sair</button></div><div id="sub"></div>
<h1 id="titulo">FinFlow</h1><div id="conteudo" class="mut">Carregando…</div></main>
<nav aria-label="Seções"><button data-a="resumo" aria-current="page">Resumo</button><button data-a="fluxo">Fluxo</button><button data-a="previsao">Previsão</button><button data-a="revisar">Revisar</button></nav>
<script>
const brl=v=>'R$ '+Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
const el=document.getElementById('conteudo'),ti=document.getElementById('titulo'),sub=document.getElementById('sub');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p){const r=await fetch('/painel/api/'+p,{credentials:'same-origin'});if(r.status===401){location.href='/painel';throw 0}return r.json()}
const vistas={
 async resumo(){const d=await api('resumo');ti.textContent='Resumo '+d.mes.slice(5)+'/'+d.mes.slice(0,4);
  sub.innerHTML=d.demonstracao?'<span class="tag">Dados de demonstração</span>':'';
  el.className='';el.innerHTML='<div class="hero"><div class="lin"><span class="r">Receitas</span><b>'+brl(d.receitas)+'</b></div>'+
  '<div class="lin"><span class="r">Gastos</span><b>'+brl(d.gastos)+'</b></div><div class="lin"><span class="r">Saldo do mês</span><span class="saldo">'+brl(d.saldo)+'</span></div></div>'+
  '<div class="card"><b>Por categoria</b>'+(d.categorias.length?d.categorias.map(c=>'<button type="button" class="cat" data-cat="'+esc(c.nome)+'" aria-label="Ver detalhes de '+esc(c.nome)+'"><div class="lin"><b>'+esc(c.nome)+'</b><span>'+
  (c.status?'<span class="pill '+c.status+'">'+({saudavel:'Saudável',atencao:'Atenção',estourado:'Estourado'})[c.status]+' · '+c.pct+'%</span>':'')+'<span class="seta" aria-hidden="true">›</span></span></div>'+
  (c.orcamento?'<div class="barra"><i style="width:'+Math.min(c.pct,100)+'%"></i></div>':'')+'<div class="mut">'+brl(c.gasto)+(c.orcamento?' / '+brl(c.orcamento):'')+'</div></button>').join(''):'<p class="mut">Nenhum gasto neste mês.</p>')+'</div>';
  el.querySelectorAll('button.cat').forEach(b=>b.onclick=()=>abrirCategoria(b.dataset.cat))},
 async fluxo(){const d=await api('fluxo');ti.textContent='Fluxo de caixa';sub.innerHTML='';el.className='';
  if(d.vazio){el.className='card';el.innerHTML='<p class="mut">Não achei contas correntes conectadas para projetar o fluxo.</p>';return}
  const dm=i=>i.slice(8,10)+'/'+i.slice(5,7);const sem=i=>['dom','seg','ter','qua','qui','sex','sáb'][new Date(i+'T12:00:00').getDay()];
  const cl=v=>v<0?'neg':'pos';const m=v=>(v<0?'-':'')+brl(Math.abs(v));
  const alerta=d.minimo<0?'<div class="alerta ruim" role="alert"><b>'+(d.saldo_inicial<0?'O saldo já está negativo':'O saldo fica negativo em '+dm(d.primeiro_negativo))+'</b><span>Menor ponto: '+m(d.minimo)+' em '+dm(d.data_minimo)+'. Para não ficar no vermelho, precisaria entrar '+brl(d.falta)+' até lá.</span></div>'
   :'<div class="alerta bom"><b>O saldo não fica negativo nos próximos '+d.horizonte+' dias</b><span>Menor ponto: '+m(d.minimo)+' em '+dm(d.data_minimo)+'.</span></div>';
  const S=d.serie,W=320,H=130,P=8,t0=new Date(S[0].data).getTime(),t1=new Date(S[S.length-1].data).getTime()||t0+1;
  const vs=S.map(x=>x.saldo),lo=Math.min(0,...vs),hi=Math.max(0,...vs),sp=(hi-lo)||1;
  const X=i=>P+(new Date(i).getTime()-t0)/((t1-t0)||1)*(W-2*P),Y=v=>P+(hi-v)/sp*(H-2*P-12),y0=Y(0);
  const pts=S.map(x=>X(x.data).toFixed(1)+','+Y(x.saldo).toFixed(1)).join(' ');
  const graf='<svg viewBox="0 0 '+W+' '+H+'" width="100%" role="img" aria-label="Saldo projetado de '+m(d.saldo_inicial)+' hoje para '+m(d.saldo_final)+' em '+dm(d.fim)+'">'+
   '<defs><clipPath id="cima"><rect x="0" y="0" width="'+W+'" height="'+y0.toFixed(1)+'"/></clipPath><clipPath id="baixo"><rect x="0" y="'+y0.toFixed(1)+'" width="'+W+'" height="'+H+'"/></clipPath></defs>'+
   '<line x1="'+P+'" x2="'+(W-P)+'" y1="'+y0.toFixed(1)+'" y2="'+y0.toFixed(1)+'" stroke="#3A5290" stroke-dasharray="3 3"/>'+
   '<polyline points="'+pts+'" fill="none" stroke="#5FE3C0" stroke-width="2.5" stroke-linejoin="round" clip-path="url(#cima)"/>'+
   '<polyline points="'+pts+'" fill="none" stroke="#FF8A7A" stroke-width="2.5" stroke-linejoin="round" clip-path="url(#baixo)"/>'+
   '<text x="'+P+'" y="'+(H-2)+'">'+dm(d.inicio)+'</text><text x="'+(W-P)+'" y="'+(H-2)+'" text-anchor="end">'+dm(d.fim)+'</text>'+
   '<text x="'+(W-P)+'" y="'+(y0-4).toFixed(1)+'" text-anchor="end">R$ 0</text></svg>';
  const soma='<div class="card soma"><h2>De onde vem o saldo de '+dm(d.fim)+'</h2>'+
   '<div class="lin"><span>Saldo hoje</span><span class="'+cl(d.saldo_inicial)+'">'+m(d.saldo_inicial)+'</span></div>'+
   '<div class="lin"><span>Entradas previstas</span><span class="pos">+'+brl(d.entradas)+'</span></div>'+
   '<div class="lin"><span>Contas e faturas</span><span>-'+brl(d.contas)+'</span></div>'+
   '<div class="lin"><span>Gastos do dia a dia<br><small class="mut">média de '+brl(d.ritmo)+' por dia'+(d.ritmo_texto?', '+esc(d.ritmo_texto):'')+'</small></span><span>-'+brl(d.gastos_dia)+'</span></div>'+
   '<div class="lin"><span>Saldo em '+dm(d.fim)+'</span><span class="'+cl(d.saldo_final)+'">'+m(d.saldo_final)+'</span></div></div>';
  const rot={conta:'Conta fixa',fatura:'Fatura do cartão',entrada:'Entrada prevista'};
  const linhas=d.dias.length?d.dias.map(x=>'<div class="dia"><div class="d">'+dm(x.data)+'<small>'+sem(x.data)+'</small></div><div>'+
   x.eventos.map(e=>'<div class="ev"><div class="n"><span>'+esc(e.nome)+'</span><span class="tipo">'+(rot[e.tipo]||'')+'</span></div><b class="'+(e.valor>0?'pos':'')+'">'+(e.valor>0?'+':'-')+brl(Math.abs(e.valor))+'</b></div>').join('')+
   '<div class="sd">Saldo depois: <b class="'+cl(x.saldo)+'">'+m(x.saldo)+'</b></div></div></div>').join(''):'<p class="mut">Nenhuma entrada ou vencimento conhecido nesse período.</p>';
  el.innerHTML=alerta+'<div class="card"><h2>Saldo projetado</h2>'+graf+'</div>'+soma+'<div class="card"><h2>Próximos movimentos</h2>'+linhas+'</div>'+
   '<details class="card"><summary>Como calculamos</summary><p>Parte do saldo atual das contas correntes. Soma as entradas recorrentes detectadas no histórico, desconta as faturas dos cartões e as contas fixas, e aplica a média de gastos do dia a dia'+(d.ritmo_texto?' ('+esc(d.ritmo_texto)+')':' dos últimos 60 dias')+'. Compras futuras no cartão e gastos fora do padrão não entram.</p></details>'},
 async previsao(){const d=await api('previsao');ti.textContent='Previsão de '+(d.vazio?'':d.mes);sub.innerHTML='';el.className='';
  if(d.vazio){ti.textContent='Previsão do mês';el.className='card';el.innerHTML='<p class="mut">Ainda não tenho gastos suficientes neste mês para prever o fechamento.</p>';return}
  const m=v=>'R$ '+Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
  const pc=d.previsto>0?Math.min(d.realizado/d.previsto*100,100):0;
  let comp='';if(d.dif!==null&&d.mes_anterior){const up=d.dif>0;comp='<div class="mut" style="font-size:13px">'+(up?'↑ ':'↓ ')+m(Math.abs(d.dif))+' ('+Math.abs(d.dif_pct)+'%) '+(up?'acima':'abaixo')+' do mês passado ('+m(d.mes_anterior)+')</div>'}
  const topo='<div class="card prev"><span class="mut" style="font-size:13px">Previsto para o fim do mês</span><span class="grande">'+m(d.previsto)+'</span>'+
   '<div class="pb" role="img" aria-label="Já gasto '+m(d.realizado)+' de '+m(d.previsto)+' previstos"><i style="left:0;width:'+pc.toFixed(1)+'%;background:#2A62F5;border-radius:5px 0 0 5px"></i><i style="left:'+pc.toFixed(1)+'%;right:0;background:#9EBBFF;border-radius:0 5px 5px 0"></i></div>'+
   '<div class="leg"><span style="--c:#2A62F5">Já gasto '+m(d.realizado)+'</span><span style="--c:#9EBBFF">Ainda por vir</span></div>'+
   '<div class="mut" style="font-size:13px">Faltam '+d.restam+' dia'+(d.restam!==1?'s':'')+' para fechar o mês</div>'+comp+'</div>';
  const est=d.categorias.filter(c=>c.status==='estoura');
  const alerta=est.length?'<div class="alerta ruim" role="alert"><b>'+est.length+(est.length>1?' categorias devem':' categoria deve')+' passar do orçamento</b><span>'+est.map(c=>esc(c.nome)+' (+'+m(c.excede)+')').join(' · ')+'</span></div>':'';
  const escala=Math.max(...d.categorias.map(c=>Math.max(c.previsto,c.limite||0)))*1.02||1;
  const rot={ok:'Dentro',atencao:'Atenção',estoura:'Estoura'};const cls={ok:'ok',atencao:'atencao',estoura:'estoura'};
  const lin=c=>{const r=c.realizado/escala*100,rest=Math.max(c.previsto-c.realizado,0)/escala*100;
   return '<div class="cp"><div class="lin"><b>'+esc(c.nome)+'</b>'+(c.limite?'<span class="pill '+cls[c.status]+'">'+rot[c.status]+'</span>':'')+'</div>'+
   '<div class="pb"><i style="left:0;width:'+r.toFixed(1)+'%;background:#2A62F5;border-radius:5px 0 0 5px"></i><i style="left:'+r.toFixed(1)+'%;width:'+rest.toFixed(1)+'%;background:#9EBBFF"></i>'+(c.limite?'<u style="left:'+(c.limite/escala*100).toFixed(1)+'%"></u>':'')+'</div>'+
   '<div class="mut" style="font-size:13px">'+m(c.previsto)+' previsto'+(c.limite?' de '+m(c.limite):'')+' · já gasto '+m(c.realizado)+'</div></div>'};
  const vis=d.categorias.slice(0,8),resto=d.categorias.slice(8);
  const lista='<div class="card"><h2>Por categoria</h2>'+vis.map(lin).join('')+(resto.length?'<details><summary>Ver mais '+resto.length+' categorias</summary>'+resto.map(lin).join('')+'</details>':'')+
   '<p class="mut" style="font-size:12px;margin:8px 0 0">Barra escura: já gasto. Barra clara: previsto até o fim do mês. Traço branco: orçamento.</p></div>';
  el.innerHTML=topo+alerta+lista+'<details class="card"><summary>Como calculamos</summary><p>Soma o que já foi gasto no mês com a média diária dos gastos de débito e Pix por categoria (últimos 60 dias), as contas fixas que ainda vão vencer e as parcelas previstas do cartão. Compras novas no cartão caem na fatura do mês seguinte e não entram aqui.</p></details>'}
, async revisar(){const d=await api('revisar');ti.textContent='Revisar lançamentos';sub.innerHTML='';el.className='';
  if(!d.itens.length){el.className='card';el.innerHTML='<b>Tudo revisado 🎉</b><p class="mut" style="margin:6px 0 0">Não há lançamentos aguardando classificação.</p>';return}
  const m=v=>'R$ '+Number(v).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
  let restam=d.total_lancamentos;const cabeca=()=>restam+(restam===1?' lançamento aguardando':' lançamentos aguardando')+' classificação'+(d.total_grupos>d.itens.length?' · mostrando os '+d.itens.length+' maiores de '+d.total_grupos+' estabelecimentos':'');
  el.innerHTML='<div class="mut" id="cab">'+cabeca()+'</div>'+d.itens.map((x,i)=>'<div class="card rev" data-k="'+esc(x.key)+'" data-n="'+x.n+'"><div class="lin"><b>'+esc(x.descricao)+'</b><b>'+m(x.total)+'</b></div>'+
   '<div class="mut" style="font-size:13px">'+[x.n>1?x.n+' lançamentos':'1 lançamento',x.datas,x.origens].filter(Boolean).map(esc).join(' · ')+'</div>'+
   '<div class="mut" style="font-size:13px">'+(x.opcoes.length?'Toque na categoria certa:':'Sem sugestão. Escolha a categoria:')+'</div>'+
   '<div class="ops">'+x.opcoes.map(o=>'<button type="button" class="op" data-c="'+esc(o.categoria)+'" data-s="'+esc(o.subcategoria)+'">'+esc(o.categoria)+' › '+esc(o.subcategoria)+'</button>').join('')+'</div>'+
   '<details'+(x.opcoes.length?'':' open')+'><summary>Outra categoria</summary><div class="cx" style="padding-bottom:6px"><select class="sc" aria-label="Categoria"></select><select class="ss" aria-label="Subcategoria"></select><button type="button" class="op ok2">Confirmar</button></div></details></div>').join('');
  const cats=(await api('categorias')).categorias;
  const preencher=sel=>{const sc=sel.querySelector('.sc'),ss=sel.querySelector('.ss');sc.innerHTML='<option value="">Categoria…</option>'+cats.map(c=>'<option>'+esc(c.nome)+'</option>').join('');ss.innerHTML='<option value="">Subcategoria…</option>';
   sc.onchange=()=>{const c=cats.find(z=>z.nome===sc.value);ss.innerHTML='<option value="">Subcategoria…</option>'+(c?c.subs.map(x=>'<option>'+esc(x)+'</option>').join(''):'')}};
  el.querySelectorAll('.rev').forEach(preencher);
  const confirmar=async(card,c,s)=>{card.querySelectorAll('button').forEach(b=>b.disabled=true);
   try{const r=await fetch('/painel/api/revisar/confirmar',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:card.dataset.k,categoria:c,subcategoria:s})});
    if(r.status===401){location.href='/painel';return}const j=await r.json();
    if(j.ok||j.motivo==='ja_resolvido'){restam=Math.max(restam-Number(card.dataset.n),0);document.getElementById('cab').textContent=restam?cabeca():'Tudo revisado 🎉';
     card.innerHTML='<div class="feito" role="status">✓ '+(j.ok?'Confirmado como '+esc(c)+' › '+esc(s):'Esse já estava resolvido')+'</div>'}
    else{card.querySelectorAll('button').forEach(b=>b.disabled=false);alert('Não consegui salvar essa categoria. Escolha outra.')}}
   catch(e){card.querySelectorAll('button').forEach(b=>b.disabled=false);alert('Não consegui salvar agora. Tente de novo.')}};
  el.onclick=ev=>{const b=ev.target.closest('button.op');if(!b)return;const card=b.closest('.rev[data-k]');if(!card)return;
   if(b.classList.contains('ok2')){const c=card.querySelector('.sc').value,s=card.querySelector('.ss').value;if(!c||!s){alert('Escolha a categoria e a subcategoria.');return}confirmar(card,c,s)}
   else confirmar(card,b.dataset.c,b.dataset.s)}}
};
const MESES=['janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'];
async function abrirCategoria(nome,semHistorico){
 if(!semHistorico)history.pushState({cat:nome},'','#'+encodeURIComponent(nome));
 window.scrollTo(0,0);ti.textContent=nome;sub.innerHTML='<button type="button" class="voltar" id="voltar">← Resumo</button>';
 document.getElementById('voltar').onclick=()=>history.back();
 el.className='mut';el.textContent='Carregando…';
 try{const d=await api('categoria?nome='+encodeURIComponent(nome));el.className='';ti.textContent=d.categoria;
  const forma={credito:'Crédito',debito:'Débito',pix:'Pix',boleto:'Boleto'};
  const dm=i=>i?i.slice(8,10)+'/'+i.slice(5,7):'';
  const topo='<div class="hero"><div class="lin"><span class="r">Gasto no mês</span><span class="saldo">'+brl(d.total)+'</span></div>'+
   (d.orcamento?'<div class="lin"><span class="r">Orçamento</span><b>'+brl(d.orcamento)+'</b></div><div class="barra"><i style="width:'+Math.min(d.pct,100)+'%"></i></div>'+
    '<div class="lin"><span class="r">'+(d.total>d.orcamento?'Passou '+brl(d.total-d.orcamento):'Sobram '+brl(d.orcamento-d.total))+'</span><span class="pill '+d.status+'">'+d.pct+'%</span></div>':'')+
   (d.dif!==null&&d.dif!==undefined?'<span class="compara '+(d.dif>0?'neg':'pos')+'">'+(d.dif>0?'↑ ':'↓ ')+brl(Math.abs(d.dif))+' '+(d.dif>0?'a mais':'a menos')+' que em '+MESES[Number(d.mes_anterior.slice(5))-1]+'</span>':
    (d.mes_anterior?'<span class="r" style="font-size:13px">Sem gastos nesta categoria em '+MESES[Number(d.mes_anterior.slice(5))-1]+'</span>':''))+
   '<span class="r" style="font-size:13px">'+d.lancamentos+(d.lancamentos===1?' lançamento':' lançamentos')+(d.previstas?' · inclui '+brl(d.previstas)+' em parcelas previstas':'')+'</span></div>'+
   (d.a_revisar?'<div class="avisorev" role="status"><span>'+d.a_revisar+(d.a_revisar===1?' lançamento aguardando':' lançamentos aguardando')+' revisão</span><button type="button" id="irrev">Revisar</button></div>':'');
  if(!d.subcategorias.length&&!d.previstas){el.innerHTML=topo+'<div class="card"><p class="mut">Nenhum gasto nesta categoria neste mês.</p></div>';return}
  const base=d.total||1;
  const subs='<div class="card"><h2>Por subcategoria</h2>'+d.subcategorias.map(s=>'<div class="sub"><div class="lin"><b>'+esc(s.nome)+'</b><b>'+brl(s.total)+'</b></div>'+
   '<div class="barra"><i style="width:'+(s.total/base*100).toFixed(1)+'%"></i></div><div class="mut" style="font-size:13px">'+Math.round(s.total/base*100)+'% da categoria · '+s.n+(s.n===1?' lançamento':' lançamentos')+'</div></div>').join('')+
   (d.previstas?'<div class="sub"><div class="lin"><b>Parcelas previstas</b><b>'+brl(d.previstas)+'</b></div><div class="mut" style="font-size:13px">Ainda não lançadas pelo banco</div></div>':'')+'</div>';
  const lista='<div class="card"><h2>Lançamentos</h2>'+d.subcategorias.map((s,k)=>'<details class="gr"'+(k===0?' open':'')+'><summary><span>'+esc(s.nome)+' · '+s.n+'</span><span class="q">'+brl(s.total)+'</span></summary>'+
   s.itens.map(i=>'<div class="it"><div class="n"><span>'+esc(i.descricao)+'</span><span class="tipo">'+[dm(i.data),forma[i.forma]||'',i.parcela?'parcela '+i.parcela:'',i.revisar?'aguardando revisão':''].filter(Boolean).join(' · ')+'</span></div><b>'+brl(i.valor)+'</b></div>').join('')+'</details>').join('')+'</div>';
  el.innerHTML=topo+subs+lista;const bR=document.getElementById('irrev');if(bR)bR.onclick=()=>ir('revisar')}
 catch(e){if(e!==0){el.className='mut';el.textContent='Não consegui carregar agora. Tente de novo em instantes.'}}}
window.addEventListener('popstate',e=>{if(e.state&&e.state.cat)abrirCategoria(e.state.cat,true);else ir('resumo')});
async function ir(a){if(location.hash)history.replaceState(null,'',location.pathname);document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-current',b.dataset.a===a?'page':'false'));
 el.className='mut';el.textContent='Carregando…';try{await vistas[a]()}catch(e){if(e!==0){el.className='mut';el.textContent='Não consegui carregar agora. Tente de novo em instantes.'}}}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>ir(b.dataset.a));
document.getElementById('sair').onclick=async()=>{await fetch('/painel/sair',{method:'POST',credentials:'same-origin'});location.href='/painel/saiu'};
ir('resumo');
</script></body></html>"""


# Cabeçalho PWA (manifesto, cor da barra, ícone do iPhone) em todas as páginas do painel + registro do service worker na principal.
_PWA_HEAD = ('<meta name="robots" content="noindex"><meta name="theme-color" content="#081226">'
             '<link rel="manifest" href="/painel/manifest.webmanifest"><link rel="apple-touch-icon" href="/painel/apple-touch-icon.png">'
             '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
             '<meta name="apple-mobile-web-app-title" content="FinFlow"><meta name="apple-mobile-web-app-status-bar-style" content="black">')
_SW_REGISTRO = "if('serviceWorker' in navigator){navigator.serviceWorker.register('/painel/sw.js',{scope:'/painel'}).catch(function(){})}"
for _n in ("_PAGINA", "_PAGINA_ENTRAR", "_PAGINA_SEM_SESSAO", "_PAGINA_EXPIRADA", "_PAGINA_SAIU", "_PAGINA_OFFLINE"):
    globals()[_n] = globals()[_n].replace('<meta name="robots" content="noindex">', _PWA_HEAD, 1)
_PAGINA = _PAGINA.replace("</script></body></html>", _SW_REGISTRO + "</script></body></html>")


# ── Aba Chat (CSS + JS em strings cruas para não escapar barras) ──
_CHAT_CSS = r""".msg{max-width:86%;padding:10px 14px;border-radius:16px;line-height:1.45;font-size:15px;overflow-wrap:anywhere;display:flex;flex-direction:column;gap:8px}
.msg.eu{align-self:flex-end;background:#2A62F5;color:#fff;border-bottom-right-radius:4px}.msg.bot{align-self:flex-start;background:var(--card);border:1px solid var(--lin);border-bottom-left-radius:4px}
.msg a{color:inherit;text-decoration:underline}#msgs{display:flex;flex-direction:column;gap:10px}
.dig{align-self:flex-start;color:var(--mut);font-size:14px;padding:4px 8px}
#comp{position:fixed;left:0;right:0;bottom:72px;background:var(--card);border-top:1px solid var(--lin);padding:8px;display:flex;gap:6px;align-items:flex-end}
#comp .in{max-width:640px;margin:0 auto;width:100%;display:flex;gap:6px;align-items:flex-end}
#comp textarea{flex:1;min-height:44px;max-height:120px;border-radius:22px;border:1px solid var(--borda);background:var(--bg);padding:11px 14px;font:inherit;font-size:16px;resize:none;color:var(--ink)}
#comp button{min-width:44px;min-height:44px;border-radius:22px;border:0;background:#16295A;color:var(--ink);cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}#comp button svg{display:block}
#comp button.env{background:#2A62F5;color:#fff;font-size:15px;font-weight:700;padding:0 16px}#gst{flex:1;display:none;flex-direction:column;justify-content:center;min-height:44px;padding-left:6px;min-width:0}#gst b{font-size:16px;color:var(--ink)}#gst b:before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;background:#B42318;margin-right:8px;animation:pisca 1s infinite}#gdica{font-size:12px;color:var(--mut)}@keyframes pisca{50%{opacity:.25}}#comp.gravando textarea,#comp.gravando #anexo,#comp.gravando #env{display:none}#comp.gravando #gst{display:flex}#comp.gravando #gcan{display:flex}#comp button.gr{background:#B42318;color:#fff}
body.chat-on main{padding-bottom:150px}.senha{display:flex;gap:6px}.senha input{flex:1;min-height:44px;border-radius:14px;border:1px solid var(--borda);background:var(--bg);color:var(--ink);padding:0 12px;font:inherit;font-size:16px}
"""
_CHAT_JS = r"""// ── Chat (mesma lógica do WhatsApp, sem Twilio) ──
let cAtivo=false,cUltimo=0,cOcupado=false,cTimer=null,cEnvio=0,cPdf=null,rec=null;
const cFmt=t=>esc(t).replace(/\*([^*\n]+)\*/g,'<b>$1</b>').replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?])/g,'$1<i>$2</i>').replace(/(https:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>').replace(/\n/g,'<br>');
function cAviso(t){const m=document.getElementById('msgs');if(!m)return;const d=document.createElement('div');d.className='dig';d.textContent=t;m.appendChild(d);setTimeout(()=>d.remove(),6000)}
async function cPost(url,opts){const r=await fetch(url,Object.assign({method:'POST',credentials:'same-origin'},opts));
 if(r.status===401){location.href='/painel';throw 0}
 if(r.status===429){cAviso('Muitas mensagens seguidas. Espere um instante.');return false}
 if(!r.ok){cAviso(r.status===413?'Esse arquivo é grande demais.':'Não consegui enviar agora. Tente de novo.');return false}
 cEnvio=Date.now();cOcupado=true;cPoll(true);return true}
function cRender(ms){const box=document.getElementById('msgs');if(!box)return;
 const perto=(window.innerHeight+window.scrollY)>=document.body.scrollHeight-160;
 box.querySelectorAll('.dig').forEach(x=>x.remove());
 ms.forEach(m=>{cUltimo=Math.max(cUltimo,m.id);const d=document.createElement('div');d.className='msg '+(m.de==='eu'?'eu':'bot');d.dataset.id=m.id;d.innerHTML='<div>'+cFmt(m.texto)+'</div>';
  if(m.botoes&&m.botoes.length){const o=document.createElement('div');o.className='ops';m.botoes.forEach(b=>{const x=document.createElement('button');x.className='op';x.type='button';x.textContent=b.titulo;x.dataset.bid=b.id;x.onclick=()=>cEnviar(b.titulo,b.id);o.appendChild(x)});d.appendChild(o)}
  if(m.acao==='senha_pdf'){const f=document.createElement('form');f.className='senha';f.innerHTML='<input type="password" inputmode="numeric" autocomplete="off" placeholder="Senha do PDF" aria-label="Senha do PDF"><button class="op" type="submit">Enviar</button>';
   f.onsubmit=async e=>{e.preventDefault();const s=f.querySelector('input').value;if(!cPdf){cAviso('Anexe o PDF de novo (📎).');return}f.remove();const fd=new FormData();fd.append('arquivo',cPdf);fd.append('senha',s);await cPost('/painel/api/chat/pdf',{body:fd})};d.appendChild(f)}
  box.appendChild(d)});
 const todas=[...box.querySelectorAll('.msg')];todas.forEach((d,i)=>d.querySelectorAll('button[data-bid]').forEach(b=>b.disabled=i!==todas.length-1));
 if(!box.children.length){const d=document.createElement('div');d.className='msg bot';d.innerHTML='<div>'+cFmt('Oi! Escreva um gasto (ex.: *gastei 45 no mercado*), faça uma pergunta ou mande um áudio 🎙️. Aqui é o mesmo assistente do WhatsApp, sem custo de mensagem.')+'</div>';box.appendChild(d)}
 if(cOcupado){const d=document.createElement('div');d.className='dig';d.textContent='digitando…';box.appendChild(d)}
 if(perto||ms.length)window.scrollTo(0,document.body.scrollHeight)}
async function cPoll(rapido){if(!cAtivo)return;clearTimeout(cTimer);
 try{const d=await api('chat/mensagens?depois='+cUltimo);cOcupado=d.ocupado;cRender(d.mensagens)}catch(e){if(e===0)return}
 cTimer=setTimeout(()=>cPoll(),(cOcupado||Date.now()-cEnvio<20000)?1000:5000)}
async function cEnviar(texto,payload){texto=(texto||'').trim();if(!texto&&!payload)return;await cPost('/painel/api/chat/enviar',{headers:{'Content-Type':'application/json'},body:JSON.stringify({texto:texto,payload:payload||null})})}
function cWav(bufs,taxa){let n=0;bufs.forEach(b=>n+=b.length);const all=new Float32Array(n);let o=0;bufs.forEach(b=>{all.set(b,o);o+=b.length});
 const alvo=16000,r=taxa/alvo,len=Math.floor(n/r),pcm=new Int16Array(len);
 for(let i=0;i<len;i++){const a=Math.floor(i*r),z=Math.min(n,Math.floor((i+1)*r));let s=0,c=0;for(let j=a;j<z;j++){s+=all[j];c++}const v=Math.max(-1,Math.min(1,c?s/c:0));pcm[i]=v<0?v*32768:v*32767}
 const buf=new ArrayBuffer(44+len*2),dv=new DataView(buf),w=(p,t)=>{for(let i=0;i<t.length;i++)dv.setUint8(p+i,t.charCodeAt(i))};
 w(0,'RIFF');dv.setUint32(4,36+len*2,true);w(8,'WAVE');w(12,'fmt ');dv.setUint32(16,16,true);dv.setUint16(20,1,true);dv.setUint16(22,1,true);dv.setUint32(24,alvo,true);dv.setUint32(28,alvo*2,true);dv.setUint16(32,2,true);dv.setUint16(34,16,true);w(36,'data');dv.setUint32(40,len*2,true);
 for(let i=0;i<len;i++)dv.setInt16(44+i*2,pcm[i],true);return new Blob([buf],{type:'audio/wav'})}
const IC_MIC='<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
IC_STOP='<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>',
IC_CLIPE='<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7-7L14 4.5a3.3 3.3 0 0 1 4.7 4.7L10 18a1.7 1.7 0 0 1-2.4-2.4l7.7-7.7"/></svg>',
IC_X='<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
function cMic(g){const b=document.getElementById('mic'),c=document.getElementById('comp');if(!b||!c)return;c.classList.toggle('gravando',g);b.classList.toggle('gr',g);b.innerHTML=g?IC_STOP:IC_MIC;b.setAttribute('aria-label',g?'Parar e enviar o áudio':'Gravar áudio')}
function cRelogio(){if(!rec)return;const s=Math.floor((Date.now()-rec.t0)/1000),e=document.getElementById('gt'),h=document.getElementById('gdica');if(e)e.textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');if(h)h.textContent=rec.falou?'Para sozinho quando você parar de falar':'Fale o gasto: "gastei 45 no mercado"'}
async function cParar(enviar,motivo){if(!rec)return;const r=rec;rec=null;clearTimeout(r.timer);clearInterval(r.rel);try{r.proc.disconnect();r.src.disconnect()}catch(e){}r.stream.getTracks().forEach(t=>t.stop());const taxa=r.ctx.sampleRate;try{r.ctx.close()}catch(e){}cMic(false);
 if(!enviar){if(motivo)cAviso(motivo);return}
 if(Date.now()-r.t0<800||r.pico<0.006){cAviso('Não ouvi nada. Toque no microfone e fale o gasto.');return}
 const fd=new FormData();fd.append('audio',cWav(r.bufs,taxa),'audio.wav');await cPost('/painel/api/chat/audio',{body:fd})}
async function cGravar(){if(rec)return cParar(true);
 try{const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}}),AC=window.AudioContext||window.webkitAudioContext,ctx=new AC(),src=ctx.createMediaStreamSource(stream),proc=ctx.createScriptProcessor(4096,1,1),bufs=[];
  const r={stream,ctx,src,proc,bufs,t0:Date.now(),falou:false,pico:0,ruido:9,mudo:0};
  // Para sozinho: depois que a pessoa falou, 1,5 s de silêncio encerra e envia. Limiar = acima do ruído de fundo medido nos primeiros 0,5 s.
  proc.onaudioprocess=e=>{const x=new Float32Array(e.inputBuffer.getChannelData(0));bufs.push(x);if(rec!==r)return;let q=0;for(let i=0;i<x.length;i++)q+=x[i]*x[i];const v=Math.sqrt(q/x.length),agora=Date.now();r.pico=Math.max(r.pico,v);
   if(agora-r.t0<500){r.ruido=Math.min(r.ruido,v);return}
   const lim=Math.min(0.05,Math.max(0.015,r.ruido*2.5));
   if(v>lim){r.falou=true;r.mudo=0}
   else if(r.falou){if(!r.mudo)r.mudo=agora;else if(agora-r.mudo>1500)setTimeout(()=>cParar(true),0)}
   else if(agora-r.t0>8000)setTimeout(()=>cParar(false,'Não ouvi nada. Toque no microfone e fale o gasto.'),0)};
  src.connect(proc);proc.connect(ctx.destination);
  r.timer=setTimeout(()=>cParar(true),80000);r.rel=setInterval(cRelogio,250);rec=r;cMic(true);cRelogio()}
 catch(e){cAviso('Não consegui usar o microfone. Permita o acesso no navegador ou digite o gasto.')}}
function cMontar(){if(document.getElementById('comp'))return;const c=document.createElement('div');c.id='comp';
 c.innerHTML='<div class="in"><button type="button" id="anexo" aria-label="Anexar PDF do extrato">'+IC_CLIPE+'</button><textarea id="txt" rows="1" maxlength="1000" placeholder="Escreva aqui…" aria-label="Mensagem"></textarea><div id="gst" hidden><b id="gt">0:00</b><span id="gdica"></span></div><button type="button" id="gcan" hidden aria-label="Cancelar gravação">'+IC_X+'</button><button type="button" id="mic" aria-label="Gravar áudio">'+IC_MIC+'</button><button type="button" class="env" id="env">Enviar</button></div><input type="file" id="arq" accept="application/pdf" hidden>';
 document.body.appendChild(c);const t=document.getElementById('txt');
 const enviar=()=>{const v=t.value;t.value='';t.style.height='auto';cEnviar(v)};
 document.getElementById('env').onclick=enviar;t.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();enviar()}};t.oninput=()=>{t.style.height='auto';t.style.height=Math.min(120,t.scrollHeight)+'px'};
 document.getElementById('mic').onclick=cGravar;document.getElementById('gcan').onclick=()=>cParar(false);document.getElementById('anexo').onclick=()=>document.getElementById('arq').click();
 document.getElementById('arq').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;if(f.size>10*1024*1024){cAviso('PDF grande demais (máximo 10 MB).');return}cPdf=f;const fd=new FormData();fd.append('arquivo',f);fd.append('senha','');await cPost('/painel/api/chat/pdf',{body:fd})}}
function cSair(){cAtivo=false;clearTimeout(cTimer);cParar(false);document.body.classList.remove('chat-on');const c=document.getElementById('comp');if(c)c.remove()}
vistas.chat=async function(){ti.textContent='Chat';sub.innerHTML='';el.className='';el.innerHTML='<div id="msgs"></div>';document.body.classList.add('chat-on');cMontar();cAtivo=true;cUltimo=0;cOcupado=false;await cPoll(true)};
const irOriginal=ir;ir=async function(a){if(a!=='chat')cSair();return irOriginal(a)};
"""
_PAGINA = _PAGINA.replace("</style></head><body><main>", _CHAT_CSS + "</style></head><body><main>", 1)
_PAGINA = _PAGINA.replace('<button data-a="resumo" aria-current="page">Resumo</button>', '<button data-a="resumo" aria-current="page">Resumo</button><button data-a="chat">Chat</button>', 1)
_PAGINA = _PAGINA.replace("\nir('resumo');", "\n" + _CHAT_JS + "ir('resumo');", 1)
