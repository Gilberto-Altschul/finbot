"""Alertas de resposta rápida para o dono por WhatsApp (texto livre, só dentro da janela de 24 h).

Variável de ambiente: ALERTA_WHATSAPP (ex.: whatsapp:+5511999999999). Sem ela, nada é enviado.
Custo: mensagem de texto livre dentro da janela de 24 h não gera cobrança de conversa da Meta; fora dela a Twilio
recusa (erro 63016) e o alerta só vai para o log. Para alertar sem depender da janela é preciso um template aprovado.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import app.database as db
from app.config import get_settings

logger = logging.getLogger(__name__)

_twilio = None
_enviados: set[tuple[str, str, str]] = set()   # (usuário, mês, categoria): um aviso por categoria/mês (zera ao reiniciar o app)


def _cliente():
    global _twilio
    if _twilio is None:
        from twilio.rest import Client
        s = get_settings()
        _twilio = Client(s.twilio_account_sid, s.twilio_auth_token)
    return _twilio


def _digitos(v: str) -> str:
    return "".join(c for c in str(v) if c.isdigit())


def destino() -> str:
    d = os.environ.get("ALERTA_WHATSAPP", "").strip()
    if d and not d.startswith("whatsapp:"):
        d = "whatsapp:" + d
    return d


def eh_dono(telefone: str) -> bool:
    d = _digitos(destino())
    return bool(d) and _digitos(telefone)[-11:] == d[-11:]


def enviar_alerta(texto: str, para: str | None = None) -> bool:
    """Texto livre a `para` (padrão: o dono, ALERTA_WHATSAPP). Sem retentativa (fora da janela repetir não adianta).
    Nunca levanta exceção."""
    para = para or destino()
    if para and not para.startswith("whatsapp:"):
        para = "whatsapp:" + para
    if not para or not texto or not str(texto).strip():
        return False
    try:
        msg = _cliente().messages.create(from_=get_settings().twilio_whatsapp_number, to=para,
                                         body="⚠️ " + str(texto).strip()[:1500])
        logger.info(f"Alerta enviado | SID: {msg.sid}")
        return True
    except Exception as exc:
        if "63016" in str(exc) or "63015" in str(exc):
            logger.warning("Alerta não enviado: fora da janela de 24 h (mande qualquer mensagem ao número para reabrir).")
        else:
            logger.warning(f"Alerta não enviado ({type(exc).__name__}): {exc}")
        return False


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def orcamentos_estourados(telefone: str) -> list[dict]:
    """Categorias do mês com gasto (lançado + parcelas previstas, como no Resumo) acima do orçamento."""
    from app import dados_banco as dbk
    from app.utils import _normalize
    mes = date.today().strftime("%Y-%m")
    previstas = dbk.previstas_do_mes_por_categoria_norm(telefone)
    saida = []
    for b in db.get_all_budgets(telefone, mes):
        limite = float(b["amount"])
        if limite <= 0:
            continue
        cat = b["category"]
        gasto = db.category_total(telefone, cat) + previstas.get(_normalize(cat), 0.0)
        if gasto > limite:
            saida.append({"categoria": cat, "limite": limite, "gasto": round(gasto, 2), "pct": round(gasto / limite * 100)})
    return saida


def liberar_aviso(telefone: str, mes: str, categoria: str) -> None:
    """Depois que o usuário muda o limite de uma categoria, um novo estouro dela pode avisar de novo."""
    from app.utils import _normalize
    alvo = _normalize(categoria)
    for chave in [k for k in _enviados if k[0] == telefone and k[1] == mes and _normalize(k[2]) == alvo]:
        _enviados.discard(chave)


def alertar_orcamentos(telefone: str) -> int:
    """Avisa o PRÓPRIO usuário, uma vez por categoria e mês, das categorias estouradas (cada um no seu número).
    Devolve quantos avisos novos saíram. ALERTA_ORCAMENTO=0 desliga; usuário de demonstração não recebe."""
    if os.environ.get("ALERTA_ORCAMENTO", "1").strip() == "0":
        return 0
    from app import demo
    if demo.eh_telefone_demo(telefone):
        return 0
    try:
        estouradas = orcamentos_estourados(telefone)
    except Exception as exc:
        logger.warning(f"Alerta de orçamento: não consegui calcular ({type(exc).__name__}).")
        return 0
    mes = date.today().strftime("%Y-%m")
    novas = [e for e in estouradas if (telefone, mes, e["categoria"]) not in _enviados]
    if not novas:
        return 0
    linhas = [f"{e['categoria']}: {_brl(e['gasto'])} de {_brl(e['limite'])} ({e['pct']}%)" for e in novas]
    texto = ("Orçamento estourado" if len(novas) == 1 else f"{len(novas)} orçamentos estourados") + "\n" + "\n".join(linhas)
    if enviar_alerta(texto, demo.telefone_real(telefone)):
        for e in novas:
            _enviados.add((telefone, mes, e["categoria"]))
        return len(novas)
    return 0


def testar(telefone: str | None = None, enviar: bool = False) -> dict:
    """Teste sem esperar o cron. Sem `telefone`: lista, para cada usuário com banco, as categorias estouradas (nada é enviado).
    Com `telefone` e enviar=True: manda o aviso a esse usuário agora, ignorando o controle de repetição."""
    from app import demo
    from app.cron_sync import telefones_com_banco, _mascara
    fones = [telefone] if telefone else telefones_com_banco()
    saida = []
    for f in fones:
        try:
            est = orcamentos_estourados(f)
        except Exception as exc:
            saida.append({"usuario": _mascara(f), "erro": type(exc).__name__})
            continue
        item = {"usuario": _mascara(f), "estouradas": est, "demo": demo.eh_telefone_demo(f)}
        if enviar and telefone and est and not item["demo"]:
            linhas = [f"{e['categoria']}: {_brl(e['gasto'])} de {_brl(e['limite'])} ({e['pct']}%)" for e in est]
            texto = ("TESTE · Orçamento estourado" if len(est) == 1 else f"TESTE · {len(est)} orçamentos estourados") + "\n" + "\n".join(linhas)
            item["enviado"] = enviar_alerta(texto, demo.telefone_real(f))
        saida.append(item)
    return {"usuarios": saida}


# ───────────────────────── Resumo semanal ─────────────────────────
def _gastos_por_compra(telefone: str, ini: date, fim: date) -> list[dict]:
    """Gastos pela DATA DA COMPRA (como a tendência semanal do bot). Parcelas 2/N em diante ficam de fora,
    para uma compra parcelada pesar uma vez só, na semana em que foi feita."""
    res = (db.get_db().table("finbot_expenses").select("category, amount, installment_of")
           .ilike("user_phone", db._q(telefone)).eq("movement", "expense")
           .gte("purchase_date", ini.isoformat()).lte("purchase_date", fim.isoformat()).limit(5000).execute())
    return [r for r in (res.data or []) if not r.get("installment_of") or int(r["installment_of"]) <= 1]


def resumo_semanal_texto(telefone: str, hoje: date | None = None) -> str | None:
    """Texto do resumo dos últimos 7 dias (hoje incluído) contra os 7 anteriores. None se não houve gasto."""
    from datetime import timedelta
    hoje = hoje or date.today()
    ini, ini_ant = hoje - timedelta(days=6), hoje - timedelta(days=13)
    atual = _gastos_por_compra(telefone, ini, hoje)
    if not atual:
        return None
    anterior = _gastos_por_compra(telefone, ini_ant, ini - timedelta(days=1))
    total = sum(float(r["amount"]) for r in atual)
    total_ant = sum(float(r["amount"]) for r in anterior)
    por_cat: dict[str, float] = {}
    for r in atual:
        c = (r.get("category") or "Outros").strip() or "Outros"
        por_cat[c] = por_cat.get(c, 0.0) + float(r["amount"])
    top = sorted(por_cat.items(), key=lambda x: -x[1])[:3]
    linhas = [f"📊 *Resumo da semana* ({ini.strftime('%d/%m')} a {hoje.strftime('%d/%m')})", "",
              f"Gastos: *{_brl(total)}*"]
    if total_ant > 0:
        dif = total - total_ant
        pct = abs(dif) / total_ant * 100
        linhas.append(f"{'↑' if dif > 0 else '↓'} {pct:.0f}% {'a mais' if dif > 0 else 'a menos'} que a semana anterior ({_brl(total_ant)})")
    else:
        linhas.append("Sem gastos na semana anterior para comparar.")
    linhas += ["", "Onde mais pesou:"] + [f"{i}. {c}: {_brl(v)}" for i, (c, v) in enumerate(top, 1)]
    try:
        linhas += ["", f"No mês até agora: {_brl(db.monthly_total(telefone))}"]
    except Exception:
        pass
    return "\n".join(linhas)


def enviar_resumo_semanal(telefone: str) -> bool:
    from app import demo
    if demo.eh_telefone_demo(telefone):
        return False
    texto = resumo_semanal_texto(telefone)
    if not texto:
        return False
    para = demo.telefone_real(telefone)
    if not para.startswith("whatsapp:"):
        para = "whatsapp:" + para
    try:
        msg = _cliente().messages.create(from_=get_settings().twilio_whatsapp_number, to=para, body=texto)
        logger.info(f"Resumo semanal enviado | SID: {msg.sid}")
        return True
    except Exception as exc:
        if "63016" in str(exc) or "63015" in str(exc):
            logger.warning("Resumo semanal não enviado: fora da janela de 24 h.")
        else:
            logger.warning(f"Resumo semanal não enviado ({type(exc).__name__}): {exc}")
        return False


def resumos_semanais_todos() -> dict:
    """Envia o resumo a cada usuário com banco. RESUMO_SEMANAL=0 desliga."""
    from app.cron_sync import telefones_com_banco, _mascara
    if os.environ.get("RESUMO_SEMANAL", "1").strip() == "0":
        return {"desligado": True}
    r = {"usuarios": 0, "enviados": 0}
    for f in telefones_com_banco():
        r["usuarios"] += 1
        try:
            if enviar_resumo_semanal(f):
                r["enviados"] += 1
        except Exception as exc:
            logger.warning(f"Resumo semanal {_mascara(f)} falhou ({type(exc).__name__}).")
    return r
