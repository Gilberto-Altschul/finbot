"""Painel web do FinFlow (esqueleto): lê os mesmos dados do WhatsApp, em tela.

Entrada: o comando `painel` no WhatsApp gera um LINK DE USO ÚNICO (vale 10 min). Abrir o link troca o código por uma
sessão (cookie HttpOnly, 12 h). Nada de senha: quem recebe o link é quem tem o WhatsApp.

Só funciona com FINBOT_PUBLIC_URL definida (é ela que monta o link). Sem ela, o comando avisa e as rotas respondem 503.
Códigos e sessões vivem na memória do processo (1 instância): se o app reiniciar, é só pedir `painel` de novo.
Todas as rotas de dados exigem a sessão e usam o mesmo telefone do WhatsApp (ou o usuário do modo demo).
"""
from __future__ import annotations

import html
import logging
import os
import secrets
import time
from datetime import date

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import app.database as db
import app.demo as demo
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


def telefone_da_sessao(sid: str | None) -> str | None:
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
    cats = []
    for r in sorted(db.monthly_by_category(user_phone, mes), key=lambda x: -x["total"]):
        limite = orc.get(_normalize(r["category"]))
        pct = round(r["total"] / limite * 100) if limite else None
        status = None if pct is None else ("estourado" if pct > 100 else ("atencao" if pct >= 80 else "saudavel"))
        cats.append({"nome": r["category"], "gasto": r["total"], "orcamento": limite, "pct": pct, "status": status})
    return {"mes": mes, "receitas": receitas, "gastos": gastos, "saldo": round(receitas - gastos, 2),
            "demonstracao": demo.eh_telefone_demo(user_phone), "categorias": cats}


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
    resp.set_cookie(COOKIE, criar_sessao(telefone), max_age=VALIDADE_SESSAO, httponly=True, samesite="lax",
                    secure=base_url().startswith("https://"), path="/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/painel", response_class=HTMLResponse)
def pagina(finflow_sessao: str | None = Cookie(default=None)):
    if not ligado():
        raise HTTPException(status_code=503, detail="painel indisponível")
    if not telefone_da_sessao(finflow_sessao):
        logger.info("Painel: acesso sem sessão (cookie ausente, bloqueado ou sessão vencida).")
        return HTMLResponse(_PAGINA_SEM_SESSAO, status_code=401, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_PAGINA, headers={"Cache-Control": "no-store"})


@router.get("/painel/api/resumo")
def api_resumo(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    return _json(dados_resumo(usuario))


@router.get("/painel/api/fluxo")
def api_fluxo(dias: int = 30, finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    import app.dados_banco as dados_banco       # import tardio: puxa o módulo de contas/faturas só quando usa
    return _json({"texto": dados_banco.fluxo_caixa(usuario, max(7, min(dias, 90)))})


@router.get("/painel/api/previsao")
def api_previsao(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    import app.dados_banco as dados_banco
    return _json({"texto": dados_banco.previsao_mes(usuario)})


@router.post("/painel/sair")
def sair(request: Request, finflow_sessao: str | None = Cookie(default=None)):
    _SESSOES.pop(finflow_sessao or "", None)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ── páginas ──────────────────────────────────────────────────────────────────
_ESTILO = """<style>body{margin:0;font-family:system-ui,sans-serif;background:#F3F5F2;color:#15221E;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
main{max-width:360px;display:flex;flex-direction:column;gap:12px}h1{font-size:24px;margin:0}p{margin:0;line-height:1.5;color:#5B6B65}
button{min-height:52px;border:0;border-radius:12px;background:#0E6B57;color:#fff;font:inherit;font-size:16px;font-weight:700;cursor:pointer}</style>"""
_PAGINA_EXPIRADA = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Link vencido ou já usado</h1>"
    "<p>Mande <b>painel</b> no WhatsApp para receber um link novo. Cada link vale por 10 minutos e abre uma única vez.</p></main></body></html>")
_PAGINA_SEM_SESSAO = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Sessão não encontrada</h1>"
    "<p>Mande <b>painel</b> no WhatsApp para receber um link novo. Se você acabou de abrir um link e caiu aqui, abra o link no navegador do celular (Chrome ou Safari), "
    "não no navegador interno de outro aplicativo, e permita cookies.</p></main></body></html>")
_PAGINA_ENTRAR = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Abrir o seu painel</h1>"
    "<p>Toque no botão para entrar. O link abre uma única vez.</p>"
    "<form method=\"post\" action=\"/painel/entrar\"><input type=\"hidden\" name=\"c\" value=\"__CODIGO__\">"
    "<button type=\"submit\" style=\"width:100%\">Abrir meu painel</button></form></main></body></html>")

_PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>FinFlow</title>
<style>
:root{--v:#0E6B57;--ink:#15221E;--mut:#5B6B65;--bg:#F3F5F2;--card:#fff;--lin:#E6EAE7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
main{max-width:640px;margin:0 auto;padding:24px 16px 96px;display:flex;flex-direction:column;gap:16px}
h1{margin:0;font-size:28px;letter-spacing:-.5px}.mut{color:var(--mut);font-size:14px}
.card{background:var(--card);border-radius:16px;padding:16px}
.hero{background:#0E3B31;color:#fff;border-radius:16px;padding:20px;display:flex;flex-direction:column;gap:10px}
.lin{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.hero .r{color:#BFD6CD}.saldo{font-size:30px;font-weight:700}
.tag{display:inline-block;padding:4px 10px;border-radius:99px;background:#E3ECE8;color:#24473D;font-size:12px}
.cat{padding:14px 0;border-bottom:1px solid var(--lin);display:flex;flex-direction:column;gap:8px}.cat:last-child{border:0}
.barra{height:8px;border-radius:4px;background:var(--lin);overflow:hidden}.barra i{display:block;height:8px;background:var(--v)}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700}
.saudavel{background:#E3F1EA;color:#0E6B57}.atencao{background:#FFF4E5;color:#7A3B00}.estourado{background:#FDECEA;color:#8A1C12}
pre{white-space:pre-wrap;font:inherit;line-height:1.5;margin:0}
nav{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #DCE2DE;display:flex;padding:6px 8px 10px}
nav button{flex:1;min-height:56px;border:0;background:none;font:inherit;font-size:14px;font-weight:600;color:var(--mut);cursor:pointer}
nav button[aria-current=page]{color:var(--v)}button.sair{border:0;background:none;color:var(--v);font:inherit;font-weight:700;min-height:44px;cursor:pointer}
</style></head><body><main>
<div class="lin"><span class="mut" id="sub"></span><button class="sair" id="sair" type="button">Sair</button></div>
<h1 id="titulo">FinFlow</h1><div id="conteudo" class="mut">Carregando…</div></main>
<nav aria-label="Seções"><button data-a="resumo" aria-current="page">Resumo</button><button data-a="fluxo">Fluxo</button><button data-a="previsao">Previsão</button></nav>
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
  '<div class="card"><b>Por categoria</b>'+(d.categorias.length?d.categorias.map(c=>'<div class="cat"><div class="lin"><b>'+esc(c.nome)+'</b>'+
  (c.status?'<span class="pill '+c.status+'">'+({saudavel:'Saudável',atencao:'Atenção',estourado:'Estourado'})[c.status]+' · '+c.pct+'%</span>':'')+'</div>'+
  (c.orcamento?'<div class="barra"><i style="width:'+Math.min(c.pct,100)+'%"></i></div>':'')+'<div class="mut">'+brl(c.gasto)+(c.orcamento?' / '+brl(c.orcamento):'')+'</div></div>').join(''):'<p class="mut">Nenhum gasto neste mês.</p>')+'</div>'},
 async fluxo(){const d=await api('fluxo');ti.textContent='Fluxo de caixa';sub.innerHTML='';el.className='card';el.innerHTML='<pre>'+esc(d.texto)+'</pre>'},
 async previsao(){const d=await api('previsao');ti.textContent='Previsão do mês';sub.innerHTML='';el.className='card';el.innerHTML='<pre>'+esc(d.texto)+'</pre>'}};
async function ir(a){document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-current',b.dataset.a===a?'page':'false'));
 el.className='mut';el.textContent='Carregando…';try{await vistas[a]()}catch(e){if(e!==0){el.className='mut';el.textContent='Não consegui carregar agora. Tente de novo em instantes.'}}}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>ir(b.dataset.a));
document.getElementById('sair').onclick=async()=>{await fetch('/painel/sair',{method:'POST',credentials:'same-origin'});location.href='/painel'};
ir('resumo');
</script></body></html>"""
