# app/merchant_stats.py
# Memória por usuário: o que ELE respondeu para cada estabelecimento.
# Só vira automático quando o histórico é consistente (>=2 respostas e >=90% iguais).
# Estabelecimentos "de tudo" (Mercado Livre...) ficam ambíguos naturalmente,
# porque as respostas do usuário divergem.
import logging
from dataclasses import dataclass, field

import app.database as db
from app.utils import get_lookup_prefix

logger = logging.getLogger(__name__)

TABELA = "finbot_user_merchant_stats"
MIN_AMOSTRAS = 2            # estabelecimento comum: 1 resposta do usuário já pesa 2 (ver revisao.responder)
MIN_AMOSTRAS_FAMILIA = 4    # marketplace (vende de tudo): só automatiza após 4 respostas iguais
MIN_CONCORDANCIA = 0.9


@dataclass
class Historico:
    total: int = 0
    top: tuple[str, str] | None = None
    share: float = 0.0
    ranking: list[tuple[str, str, int]] = field(default_factory=list)


def chave_familia(familia: str) -> str:
    return f"familia:{familia}"


def resumir_historico(rows: list[dict]) -> Historico:  # pura
    ranking = sorted(((r["category"], r["subcategory"], int(r["n"])) for r in rows), key=lambda x: -x[2])
    total = sum(n for _, _, n in ranking)
    if not total:
        return Historico()
    cat, sub, n = ranking[0]
    return Historico(total, (cat, sub), n / total, ranking)


def e_consistente(h: Historico, minimo: int = MIN_AMOSTRAS) -> bool:  # pura
    return h.total >= minimo and h.share >= MIN_CONCORDANCIA


def ler_historico(user_phone: str, merchant_key: str) -> Historico:
    try:
        res = (db.get_db().table(TABELA).select("category, subcategory, n")
               .eq("user_key", get_lookup_prefix(user_phone))
               .eq("merchant_key", merchant_key).execute())
        return resumir_historico(res.data or [])
    except Exception as e:
        logger.error(f"Erro ao ler histórico de '{merchant_key}': {e}")
        return Historico()


def historico_de_confirmados(rows: list[dict], peso: int = MIN_AMOSTRAS) -> Historico:  # pura
    """Lançamentos que o USUÁRIO confirmou (review_status='confirmado') viram histórico: cada um pesa `peso`."""
    cont: dict[tuple[str, str], int] = {}
    for r in rows:
        c, sc = r.get("category"), r.get("subcategory")
        if c and sc:
            cont[(c, sc)] = cont.get((c, sc), 0) + peso
    return resumir_historico([{"category": c, "subcategory": sc, "n": n} for (c, sc), n in cont.items()])


def ler_historico_destinatario(user_phone: str, merchant_key: str) -> Historico:
    """Pix/TED para pessoa: o banco corta o nome de formas diferentes (PDF do Itaú: "GILVANI"; Pluggy: "GILVANIA RIBEIRO
    GOMES"), então a chave exata falha. Procura por PREFIXO da chave, primeiro nas respostas aprendidas e depois nos
    lançamentos que o usuário já confirmou (inclusive os antigos, gravados sem chave)."""
    chave = (merchant_key or "").strip()
    if len(chave) < 5 or chave.startswith("familia:"):
        return Historico()
    try:
        cli = db.get_db()
        res = (cli.table(TABELA).select("category, subcategory, n")
               .eq("user_key", get_lookup_prefix(user_phone)).like("merchant_key", f"{chave}%").execute())
        h = resumir_historico(res.data or [])
        if h.total:
            return h
        conf = (cli.table("finbot_expenses").select("category, subcategory")
                .ilike("user_phone", f"{get_lookup_prefix(user_phone)}:%")
                .eq("review_status", "confirmado").ilike("description", f"%{chave}%").limit(50).execute())
        return historico_de_confirmados(conf.data or [])
    except Exception as e:
        logger.error(f"Erro ao ler histórico do destinatário '{chave}': {e}")
        return Historico()


def registrar_resposta(user_phone: str, merchant_key: str, categoria: str, subcategoria: str, peso: int = 1) -> None:
    """Soma `peso` (atômico, via função SQL). Estabelecimento comum: peso=MIN_AMOSTRAS (uma resposta
    já vira regra). Marketplace: peso=1 — só vira automático se o usuário repetir a mesma escolha."""
    try:
        db.get_db().rpc("finbot_incr_merchant_stat", {
            "p_user_key": get_lookup_prefix(user_phone), "p_merchant_key": merchant_key,
            "p_category": categoria, "p_subcategory": subcategoria, "p_n": peso,
        }).execute()
    except Exception as e:
        logger.error(f"Erro ao registrar resposta para '{merchant_key}': {e}")
