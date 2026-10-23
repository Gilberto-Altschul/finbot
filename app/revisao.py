# app/revisao.py
# Fila de revisão: as transações JÁ estão salvas (review_status='revisar'); aqui só
# perguntamos, UMA por vez, começando pelas de maior valor, e cada resposta ensina.
# O estado da conversa fica em finbot_user_connections (status + pending_transactions),
# sem tabela nova e sem expirar: se o usuário sumir, nada se perde.
import json
import re
import uuid
import logging
from dataclasses import dataclass, field
from datetime import date

import app.database as db
import app.rotina as rotina
from app.merchant import normalizar_merchant
from app.merchant_stats import ler_historico, registrar_resposta, MIN_AMOSTRAS

logger = logging.getLogger(__name__)

STATUS_REVISAO = "aguardando_revisao"
PERGUNTAS_POR_SESSAO = 5
# Estados em que a coluna `status` já pertence a outro fluxo (senha de PDF, processamento...).
_STATUS_OCUPADOS = ("aguardando_senha", "processando")


@dataclass
class Grupo:
    key: str                  # chave de agrupamento ("tx:<id>" p/ marketplace; merchant_key nos demais)
    learn_key: str            # chave onde a resposta é aprendida
    ids: list[int] = field(default_factory=list)
    descricao: str = ""
    total: float = 0.0
    atual: tuple[str, str] = ("Outros", "Outros")
    familia: bool = False
    datas: list[str] = field(default_factory=list)   # purchase_date (YYYY-MM-DD) de cada lançamento

    @property
    def n(self) -> int:
        return len(self.ids)


# ── Lógica pura ──────────────────────────────────────────────────────────────

def agrupar_pendentes(rows: list[dict], pulados: set[str] | None = None) -> list[Grupo]:
    """Uma pergunta por estabelecimento (12 compras na mesma padaria = 1 pergunta).
    Marketplaces (familia:*) nunca agrupam: cada compra é uma pergunta."""
    pulados = pulados or set()
    grupos: dict[str, Grupo] = {}
    for r in rows:
        mk = r.get("merchant_key") or normalizar_merchant(r.get("description", ""))
        familia = mk.startswith("familia:")
        key = f"tx:{r['id']}" if familia else mk
        if key in pulados:
            continue
        g = grupos.setdefault(key, Grupo(key=key, learn_key=mk, descricao=r.get("description", ""), familia=familia))
        g.ids.append(r["id"])
        g.total += float(r.get("amount") or 0)
        if r.get("purchase_date"):
            g.datas.append(str(r["purchase_date"])[:10])
        if g.atual == ("Outros", "Outros") and r.get("category") not in (None, "Outros"):
            g.atual = (r["category"], r.get("subcategory") or "Outros")
    return sorted(grupos.values(), key=lambda g: -g.total)


_ALTERNATIVAS = {("Alimentação", "Restaurante"): ("Lazer", "Restaurante lazer")}


def montar_opcoes(g: Grupo, ranking: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """Até 3 botões: primeiro o que ELE já usou nesse lugar, depois o palpite do sistema."""
    ops = [(c, s) for c, s, _ in ranking]
    if g.atual[0] != "Outros" and g.atual not in ops:
        ops.append(g.atual)
    alt = _ALTERNATIVAS.get(g.atual)   # restaurante: a outra face da dúvida (rotina x lazer)
    if alt and alt not in ops:
        ops.append(alt)
    return ops[:3]


def _brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_data(iso: str, hoje: date | None = None) -> str:
    """'2026-10-15' -> '15/10' (com o ano quando não é o ano corrente)."""
    try:
        d = date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return ""
    ano = (hoje or date.today()).year
    return d.strftime("%d/%m") if d.year == ano else d.strftime("%d/%m/%Y")


def fmt_datas(datas: list[str], hoje: date | None = None) -> str:
    """Uma data -> '15/10'; várias -> '02/10 a 15/10'; sem data -> ''."""
    validas = sorted({d[:10] for d in datas if d and _fmt_data(d, hoje)})
    if not validas:
        return ""
    if len(validas) == 1:
        return _fmt_data(validas[0], hoje)
    return f"{_fmt_data(validas[0], hoje)} a {_fmt_data(validas[-1], hoje)}"


def cabeca_pergunta(g: Grupo) -> str:
    """'*Padaria X* — R$ 25,00 · 15/10' (vários lançamentos: '(3 lançamentos · 02/10 a 15/10)')."""
    datas = fmt_datas(g.datas)
    if g.n > 1:
        extra = f" ({g.n} lançamentos" + (f" · {datas}" if datas else "") + ")"
    else:
        extra = f" · {datas}" if datas else ""
    return f"*{g.descricao}* — {_brl(g.total)}{extra}"


def texto_pergunta(g: Grupo, opcoes: list[tuple[str, str]]) -> str:
    linhas = [f"🤔 {cabeca_pergunta(g)}", ""]
    for i, (c, s) in enumerate(opcoes, 1):
        linhas.append(f"*{i}* · {c} › {s}")
    linhas.append("✏️ ou digite uma palavra (ex: _mercado_, _uber_)")
    linhas.append("*0* · pular")
    return "\n".join(linhas)


def interpretar_resposta(texto: str, opcoes: list[tuple[str, str]], resolver) -> tuple[str, tuple[str, str] | None]:
    """('escolha', (cat, sub)) | ('pular', None) | ('invalida', None). `resolver` = _resolver_categoria."""
    t = (texto or "").strip().lower()
    if t in ("0", "pular", "depois", "pula"):
        return "pular", None
    if t.isdigit() and 1 <= int(t) <= len(opcoes):
        return "escolha", opcoes[int(t) - 1]
    achado = resolver(t)  # palavra do dia a dia; "99" fora do intervalo cai aqui (Uber 99)
    if achado and achado[0] != "Outros":
        return "escolha", achado
    return "invalida", None


# Comandos do bot (agent._KEYWORD_TOOLS) que nunca podem ser lidos como resposta de revisão.
_COMANDOS_RESERVADOS = {
    "acertar", "ajuda", "cartao", "cartão", "categorias", "extrato", "fatura", "gasto", "gastos",
    "limite", "limites", "lista", "listar", "orcamento", "orçamento", "parcelamentos", "receitas",
    "resumo", "saldo", "sincronizar", "sync", "tendencia", "ver", "revisar",
}


# Aberturas típicas de conversa/pergunta: nunca são resposta à revisão.
_INICIOS_DE_CONVERSA = {
    "oi", "olá", "ola", "bom", "boa", "obrigado", "obrigada", "valeu", "quanto", "quando", "qual",
    "quais", "como", "onde", "quem", "por", "que", "o", "me", "meu", "minha", "gastei", "paguei",
    "comprei", "recebi", "apagar", "excluir", "deletar", "cancelar", "editar", "alterar", "mudar",
    "sim", "não", "nao", "ok", "teste",
}


def eh_resposta_de_revisao(texto: str, resolver) -> bool:
    """Evita capturar conversa normal: só números curtos ou palavras que o dicionário reconhece."""
    t = (texto or "").strip().lower()
    if not t or len(t) > 40:
        return False
    if t.isdigit() or t in ("pular", "depois", "pula"):
        return True
    if t in _COMANDOS_RESERVADOS:
        return False
    # Só palavra EXATA do dicionário: com fuzzy, "saldo" virava "salão" e sequestrava o comando.
    from app.ingestion import resolver_exato
    achado = resolver_exato(t)
    if achado:
        return achado[0] != "Outros"
    # Frase curta e sem número ("lava rápido") com pergunta aberta é quase certamente a resposta:
    # captura e, se não reconhecer, o responder pede de novo em vez de tratar como gasto novo.
    palavras = t.split()
    if (len(palavras) <= 3 and not re.search(r"\d|\?", t)
            and palavras[0] not in _INICIOS_DE_CONVERSA):
        return True
    return False


# ── Estado (finbot_user_connections) ─────────────────────────────────────────

# Atenção à ordem da API do Supabase: filtros (ilike/eq/...) só existem DEPOIS de select()/update().
def _sel(user_phone: str, colunas: str):
    return db.get_db().table("finbot_user_connections").select(colunas).ilike("user_phone", db._q(user_phone))


def _upd(user_phone: str, dados: dict):
    return db.get_db().table("finbot_user_connections").update(dados).ilike("user_phone", db._q(user_phone))


def ler_estado(user_phone: str) -> dict | None:
    try:
        res = _sel(user_phone, "pending_transactions, status").eq("status", STATUS_REVISAO).limit(1).execute()
        if res.data and res.data[0].get("pending_transactions"):
            return json.loads(res.data[0]["pending_transactions"])
    except Exception as e:
        logger.error(f"Erro ao ler estado de revisão: {e}")
    return None


def _salvar_estado(user_phone: str, estado: dict) -> None:
    _upd(user_phone, {"pending_transactions": json.dumps(estado, ensure_ascii=False),
                      "status": STATUS_REVISAO}).execute()


def _limpar_estado(user_phone: str) -> None:
    _upd(user_phone, {"pending_transactions": None, "status": "ativo"}).eq("status", STATUS_REVISAO).execute()


def status_atual(user_phone: str) -> str | None:
    try:
        res = _sel(user_phone, "status").limit(1).execute()
        return res.data[0].get("status") if res.data else None
    except Exception as e:
        logger.error(f"Erro ao ler status: {e}")
        return None


def _buscar_pendentes(user_phone: str) -> list[dict]:
    res = (db.get_db().table("finbot_expenses")
           .select("id, description, amount, merchant_key, category, subcategory, purchase_date")
           .ilike("user_phone", db._q(user_phone))
           .eq("review_status", "revisar").eq("transaction_type", "expense")
           .limit(1000).execute())
    return res.data or []


# ── Fluxo ────────────────────────────────────────────────────────────────────

def iniciar(user_phone: str, faltam: int = PERGUNTAS_POR_SESSAO, pulados: list[str] | None = None) -> str | None:
    """Monta a próxima pergunta (e guarda o estado). None = nada a revisar."""
    rows = _buscar_pendentes(user_phone)
    grupos = agrupar_pendentes(rows, set(pulados or []))
    if faltam <= 0 or not grupos:
        _limpar_estado(user_phone)
        if rows:  # limite da sessão atingido, ou o que sobrou foi pulado: nada se perde
            return (f"👍 Por hoje é isso. {len(rows)} lançamento(s) seguem como *a revisar* no seu painel.")
        return None
    g = grupos[0]
    ops = montar_opcoes(g, ler_historico(user_phone, g.learn_key).ranking)
    texto = texto_pergunta(g, ops)
    cabeca = cabeca_pergunta(g)
    _salvar_estado(user_phone, {
        "key": g.key, "learn_key": g.learn_key, "ids": g.ids, "familia": g.familia,
        "opcoes": [list(o) for o in ops], "faltam": faltam, "pulados": pulados or [],
        "qid": uuid.uuid4().hex[:6], "texto": texto, "cabeca": cabeca,
    })
    return texto


def botoes(estado: dict) -> list[tuple[str, str]]:
    """[(id, título)] para o WhatsApp: até 2 categorias + Pular (limite de 3 na janela de 24h).
    O id leva o código da pergunta (qid): toque em botão de pergunta antiga é descartado."""
    ops = estado.get("opcoes") or []
    qid = estado.get("qid")
    if not ops or not qid:
        return []
    bts = [(f"r:{qid}:{i}", sub[:20]) for i, (_, sub) in enumerate(ops[:2], 1)]
    return bts + [(f"r:{qid}:0", "Pular")]


def resolver_payload(user_phone: str, payload: str) -> str | None:
    """'r:<qid>:<n>' -> 'n' se for da pergunta ATUAL; None se for de uma pergunta antiga."""
    try:
        _, qid, n = payload.split(":")
    except ValueError:
        return None
    est = ler_estado(user_phone)
    return n if est and est.get("qid") == qid else None


def _como_inteiro(v) -> int | None:
    """Converte para int só se for número de verdade (None, "None", "" -> None)."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def responder(user_phone: str, texto: str) -> str:
    from app.ingestion import _resolver_categoria  # import tardio: ingestion também importa este módulo
    est = ler_estado(user_phone)
    if not est:
        return "Não há nada pendente de revisão agora. 😉"
    ops = [tuple(o) for o in est["opcoes"]]
    acao, escolha = interpretar_resposta(texto, ops, _resolver_categoria)

    if acao == "invalida":
        return "Não entendi essa categoria 🤔 Responda com o número, uma palavra que eu conheça (ex: _mercado_, _oficina_) ou *0* para pular."

    if acao == "pular":
        proxima = iniciar(user_phone, est["faltam"], est["pulados"] + [est["key"]])
        return proxima or "Ok, deixei para depois. ✅"

    cat, sub = escolha
    eh_refeicao = sub in (rotina.SUB_ROTINA, rotina.SUB_LAZER)
    if eh_refeicao:
        rotina.invalidar(user_phone)
        antes = rotina.perfil(user_phone)
    peso = 1 if est["familia"] else MIN_AMOSTRAS   # marketplace precisa se repetir; o resto aprende de 1 resposta
    etapa = "buscar subcategoria"
    try:
        sub_id = _como_inteiro(db.get_subcategory_id_by_name(sub))   # só número válido vai para a coluna bigint
        campos = {"category": cat, "subcategory": sub, "review_status": "confirmado", "confidence": 1.0}
        if sub_id:
            campos["subcategory_id"] = sub_id
        etapa = "atualizar lançamentos"
        ids = [i for i in (_como_inteiro(x) for x in est["ids"]) if i]   # descarta None/"None"
        for i in range(0, len(ids), 50):
            (db.get_db().table("finbot_expenses").update(campos)
             .in_("id", ids[i:i + 50]).ilike("user_phone", db._q(user_phone)).execute())
    except Exception as exc:
        raise RuntimeError(f"etapa '{etapa}' ({cat}/{sub}, sub_id={sub_id if etapa != 'buscar subcategoria' else '?'}, "
                           f"ids={est['ids'][:5]}): {exc}") from exc
    registrar_resposta(user_phone, est["learn_key"], cat, sub, peso)

    n = len(est["ids"])
    confirmacao = f"✅ Anotado: {cat} › {sub}." + (f" Reclassifiquei {n} lançamentos." if n > 1 else "")
    if eh_refeicao:
        rotina.invalidar(user_phone)
        depois = rotina.perfil(user_phone)
        if depois != antes:   # só informa, não pergunta: o usuário foi consistente o bastante para o bot aprender
            confirmacao += f"\n💡 Aprendi: {depois.descricao()}. Não pergunto mais nesses casos."
    proxima = iniciar(user_phone, est["faltam"] - 1, est["pulados"])
    return f"{confirmacao}\n\n{proxima}" if proxima else confirmacao + " Tudo revisado! 🎉"


def retomar(user_phone: str) -> str:
    """Comando "revisar": o usuário pede a fila de pendentes quando quiser."""
    if status_atual(user_phone) in _STATUS_OCUPADOS:
        return "Estou no meio de outro processamento. Tente de novo em instantes. ⏳"
    return iniciar(user_phone) or "Nada pendente de revisão. 🎉"


COMANDOS_REVISAR = {"revisar", "revisao", "revisão", "pendentes", "pendencias", "pendências", "revisar pendentes"}


def eh_comando_revisar(texto: str) -> bool:
    return (texto or "").strip().lower() in COMANDOS_REVISAR
