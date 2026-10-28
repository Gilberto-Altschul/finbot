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
from pydantic import BaseModel
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
        return HTMLResponse(_com_whatsapp(_PAGINA_SEM_SESSAO), status_code=401, headers={"Cache-Control": "no-store"})
    return HTMLResponse(_com_whatsapp(_PAGINA), headers={"Cache-Control": "no-store"})


@router.get("/painel/api/resumo")
def api_resumo(finflow_sessao: str | None = Cookie(default=None)):
    _real, usuario = _usuario(finflow_sessao)
    return _json(dados_resumo(usuario))


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
    "não no navegador interno de outro aplicativo, e permita cookies.</p><a href=\"__WA__\" style=\"display:flex;align-items:center;justify-content:center;min-height:52px;border-radius:12px;background:#0E6B57;color:#fff;font-weight:700;text-decoration:none\">Abrir o WhatsApp</a></main></body></html>")
_PAGINA_SAIU = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>FinFlow</title>" + _ESTILO + "</head><body><main><h1>Você saiu do painel</h1>"
    "<p>Seus dados continuam guardados. Para entrar de novo, mande <b>painel</b> no WhatsApp e abra o link novo.</p><a href=\"__WA__\" style=\"display:flex;align-items:center;justify-content:center;min-height:52px;border-radius:12px;background:#0E6B57;color:#fff;font-weight:700;text-decoration:none\">Abrir o WhatsApp</a></main></body></html>")
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
#conteudo{display:flex;flex-direction:column;gap:16px}h2{margin:0;font-size:17px}
.alerta{border-radius:16px;padding:16px;display:flex;flex-direction:column;gap:6px}.alerta.ruim{background:#FDECEA;color:#8A1C12}.alerta.bom{background:#E3F1EA;color:#0B4F40}
.alerta b{font-size:18px}.alerta span{font-size:14px;line-height:1.45}
.soma{display:flex;flex-direction:column}.soma .lin{padding:10px 0;border-bottom:1px solid var(--lin)}.soma .lin:last-child{border:0;font-weight:700;font-size:16px}
.neg{color:#B42318}.pos{color:#0E6B57}.dia{padding:14px 0;border-bottom:1px solid var(--lin);display:grid;grid-template-columns:52px 1fr;gap:4px 12px}.dia:last-child{border:0}
.dia .d{font-weight:700;font-size:15px}.dia .d small{display:block;font-weight:400;color:var(--mut);font-size:12px}
.ev{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:2px 0}.ev .n{display:flex;flex-direction:column;gap:2px;min-width:0}.ev .n span{font-size:15px;overflow-wrap:anywhere}.ev b{white-space:nowrap}.lin span:last-child{white-space:nowrap}
.tipo{font-size:12px;color:var(--mut)}.sd{font-size:13px;color:var(--mut);margin-top:4px}
details summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:700;color:var(--v)}details p{margin:0;font-size:13px;line-height:1.5;color:var(--mut)}
h3{margin:0;font-size:15px}.prev{display:flex;flex-direction:column;gap:4px}.prev .grande{font-size:30px;font-weight:700;letter-spacing:-.5px}
.pb{position:relative;height:10px;border-radius:5px;background:var(--lin)}.pb i{position:absolute;top:0;bottom:0;display:block}.pb u{position:absolute;top:-3px;bottom:-3px;width:2px;background:#15221E}
.leg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut)}.leg span:before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px;background:var(--c)}
.cp{padding:14px 0;border-bottom:1px solid var(--lin);display:flex;flex-direction:column;gap:8px}.cp:last-child{border:0}
.ok{background:#E3F1EA;color:#0E6B57}.estoura{background:#FDECEA;color:#8A1C12}
button.op{min-height:44px;padding:0 14px;border-radius:12px;border:1.5px solid var(--v);background:#fff;color:var(--v);font:inherit;font-size:14px;font-weight:700;cursor:pointer;text-align:left}
button.op:disabled{opacity:.5}select{min-height:44px;border-radius:12px;border:1.5px solid #8A9A93;background:#fff;font:inherit;font-size:15px;padding:0 10px;color:var(--ink);width:100%}
.rev,.cx{display:flex;flex-direction:column;gap:10px}.feito{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--v);font-size:14px}.ops{display:flex;flex-wrap:wrap;gap:8px}
svg text{font:11px system-ui,sans-serif;fill:#5B6B65}
pre{white-space:pre-wrap;font:inherit;line-height:1.5;margin:0}
nav{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #DCE2DE;display:flex;padding:6px 8px 10px}
nav button{flex:1;min-height:56px;border:0;background:none;font:inherit;font-size:14px;font-weight:600;color:var(--mut);cursor:pointer}
nav button[aria-current=page]{color:var(--v)}.sair{border:0;background:none;color:var(--v);font:inherit;font-weight:700;min-height:44px;cursor:pointer}a.sair{background:none}
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
  '<div class="card"><b>Por categoria</b>'+(d.categorias.length?d.categorias.map(c=>'<div class="cat"><div class="lin"><b>'+esc(c.nome)+'</b>'+
  (c.status?'<span class="pill '+c.status+'">'+({saudavel:'Saudável',atencao:'Atenção',estourado:'Estourado'})[c.status]+' · '+c.pct+'%</span>':'')+'</div>'+
  (c.orcamento?'<div class="barra"><i style="width:'+Math.min(c.pct,100)+'%"></i></div>':'')+'<div class="mut">'+brl(c.gasto)+(c.orcamento?' / '+brl(c.orcamento):'')+'</div></div>').join(''):'<p class="mut">Nenhum gasto neste mês.</p>')+'</div>'},
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
   '<line x1="'+P+'" x2="'+(W-P)+'" y1="'+y0.toFixed(1)+'" y2="'+y0.toFixed(1)+'" stroke="#9AA9A2" stroke-dasharray="3 3"/>'+
   '<polyline points="'+pts+'" fill="none" stroke="#0E6B57" stroke-width="2.5" stroke-linejoin="round" clip-path="url(#cima)"/>'+
   '<polyline points="'+pts+'" fill="none" stroke="#B42318" stroke-width="2.5" stroke-linejoin="round" clip-path="url(#baixo)"/>'+
   '<text x="'+P+'" y="'+(H-2)+'">'+dm(d.inicio)+'</text><text x="'+(W-P)+'" y="'+(H-2)+'" text-anchor="end">'+dm(d.fim)+'</text>'+
   '<text x="'+(W-P)+'" y="'+(y0-4).toFixed(1)+'" text-anchor="end">R$ 0</text></svg>';
  const soma='<div class="card soma"><h2>De onde vem o saldo de '+dm(d.fim)+'</h2>'+
   '<div class="lin"><span>Saldo hoje</span><span class="'+cl(d.saldo_inicial)+'">'+m(d.saldo_inicial)+'</span></div>'+
   '<div class="lin"><span>Entradas previstas</span><span class="pos">+'+brl(d.entradas)+'</span></div>'+
   '<div class="lin"><span>Contas e faturas</span><span>-'+brl(d.contas)+'</span></div>'+
   '<div class="lin"><span>Gastos do dia a dia<br><small class="mut">média de '+brl(d.ritmo)+' por dia</small></span><span>-'+brl(d.gastos_dia)+'</span></div>'+
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
   '<div class="pb" role="img" aria-label="Já gasto '+m(d.realizado)+' de '+m(d.previsto)+' previstos"><i style="left:0;width:'+pc.toFixed(1)+'%;background:#0E6B57;border-radius:5px 0 0 5px"></i><i style="left:'+pc.toFixed(1)+'%;right:0;background:#8FB9AC;border-radius:0 5px 5px 0"></i></div>'+
   '<div class="leg"><span style="--c:#0E6B57">Já gasto '+m(d.realizado)+'</span><span style="--c:#8FB9AC">Ainda por vir</span></div>'+
   '<div class="mut" style="font-size:13px">Faltam '+d.restam+' dia'+(d.restam!==1?'s':'')+' para fechar o mês</div>'+comp+'</div>';
  const est=d.categorias.filter(c=>c.status==='estoura');
  const alerta=est.length?'<div class="alerta ruim" role="alert"><b>'+est.length+(est.length>1?' categorias devem':' categoria deve')+' passar do orçamento</b><span>'+est.map(c=>esc(c.nome)+' (+'+m(c.excede)+')').join(' · ')+'</span></div>':'';
  const escala=Math.max(...d.categorias.map(c=>Math.max(c.previsto,c.limite||0)))*1.02||1;
  const rot={ok:'Dentro',atencao:'Atenção',estoura:'Estoura'};const cls={ok:'ok',atencao:'atencao',estoura:'estoura'};
  const lin=c=>{const r=c.realizado/escala*100,rest=Math.max(c.previsto-c.realizado,0)/escala*100;
   return '<div class="cp"><div class="lin"><b>'+esc(c.nome)+'</b>'+(c.limite?'<span class="pill '+cls[c.status]+'">'+rot[c.status]+'</span>':'')+'</div>'+
   '<div class="pb"><i style="left:0;width:'+r.toFixed(1)+'%;background:#0E6B57;border-radius:5px 0 0 5px"></i><i style="left:'+r.toFixed(1)+'%;width:'+rest.toFixed(1)+'%;background:#8FB9AC"></i>'+(c.limite?'<u style="left:'+(c.limite/escala*100).toFixed(1)+'%"></u>':'')+'</div>'+
   '<div class="mut" style="font-size:13px">'+m(c.previsto)+' previsto'+(c.limite?' de '+m(c.limite):'')+' · já gasto '+m(c.realizado)+'</div></div>'};
  const vis=d.categorias.slice(0,8),resto=d.categorias.slice(8);
  const lista='<div class="card"><h2>Por categoria</h2>'+vis.map(lin).join('')+(resto.length?'<details><summary>Ver mais '+resto.length+' categorias</summary>'+resto.map(lin).join('')+'</details>':'')+
   '<p class="mut" style="font-size:12px;margin:8px 0 0">Barra escura: já gasto. Barra clara: previsto até o fim do mês. Traço preto: orçamento.</p></div>';
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
async function ir(a){document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-current',b.dataset.a===a?'page':'false'));
 el.className='mut';el.textContent='Carregando…';try{await vistas[a]()}catch(e){if(e!==0){el.className='mut';el.textContent='Não consegui carregar agora. Tente de novo em instantes.'}}}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>ir(b.dataset.a));
document.getElementById('sair').onclick=async()=>{await fetch('/painel/sair',{method:'POST',credentials:'same-origin'});location.href='/painel/saiu'};
ir('resumo');
</script></body></html>"""
