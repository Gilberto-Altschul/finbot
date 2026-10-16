# app/classificador.py
# Decide COMO uma transação entra no banco: confirmada, automática ou "a revisar".
# Nunca bloqueia a importação esperando o usuário.
import re
from dataclasses import dataclass, field

from app.merchant import normalizar_merchant, familia_ambigua
from app.merchant_stats import (
    Historico, e_consistente, ler_historico, chave_familia, MIN_AMOSTRAS, MIN_AMOSTRAS_FAMILIA,
)

CONF_HISTORICO = 0.95   # usuário respondeu o mesmo, de forma consistente
CONF_REGRA = 0.75       # keyword/LLM sem ambiguidade
CONF_DUVIDA = 0.5       # termo ambíguo ou histórico dividido
CONF_NENHUMA = 0.0      # Outros / sem palpite útil


# Restaurante: rotina (Alimentação) ou lazer? Decide pelo contexto da compra, não pelo lugar.
# Limite e dias são o ponto de partida; app/rotina.py aprende o perfil de cada usuário.
RESTAURANTE = ("Alimentação", "Restaurante")
RESTAURANTE_LAZER = ("Lazer", "Restaurante lazer")
_TERMOS_RESTAURANTE = ["restaurante", "restaurant", "bistro", "gastronomia", "churrascaria", "pizzaria"]


@dataclass
class Decisao:
    categoria: str
    subcategoria: str
    status: str                      # 'auto' | 'revisar'
    confidence: float
    merchant_key: str                # chave usada para aprender (família OU estabelecimento)
    sugestoes: list[tuple[str, str]] = field(default_factory=list)  # até 3, para os botões


def _top3(h: Historico, extra: tuple[str, str] | None = None) -> list[tuple[str, str]]:
    out = [(c, s) for c, s, _ in h.ranking]
    if extra and extra not in out and extra[0] not in ("Outros", "Perguntar"):
        out.append(extra)
    return out[:3]


def decidir(base: tuple[str, str], familia: str | None, hist_familia: Historico,
            hist_merchant: Historico, termo_ambiguo: bool, merchant_key: str) -> Decisao:
    """Função pura: toda a política de classificação num lugar só (testável sem banco)."""
    if familia:
        chave, h = chave_familia(familia), hist_familia
    else:
        chave, h = merchant_key, hist_merchant

    if e_consistente(h, MIN_AMOSTRAS_FAMILIA if familia else MIN_AMOSTRAS):
        return Decisao(*h.top, "auto", CONF_HISTORICO, chave, _top3(h))

    if h.total > 0:  # histórico existe mas é dividido: o que ELE já usou vem primeiro
        return Decisao(*h.top, "revisar", CONF_DUVIDA, chave, _top3(h, base))

    if familia:      # marketplace sem histórico: nunca confiar no palpite da loja
        return Decisao(base[0], base[1], "revisar", CONF_NENHUMA, chave, _top3(h, base))

    if base[0] in ("Outros", "Perguntar"):
        return Decisao("Outros", "Outros", "revisar", CONF_NENHUMA, chave, [])
    if termo_ambiguo:  # restaurante/bar/padaria: Alimentação ou Lazer?
        return Decisao(base[0], base[1], "revisar", CONF_DUVIDA, chave, _top3(h, base))
    return Decisao(base[0], base[1], "auto", CONF_REGRA, chave, [base])


def tem_termo_ambiguo(descricao: str) -> bool:
    """Restaurante/bar/padaria/marketplace: o palpite existe, mas é dúvida real.
    Usa as mesmas regras do categorizer (passo 1) sem exigir nenhuma função nova nele."""
    from app.categorizer import _tem_termo, _AMBIGUOUS_TERMS
    from app.utils import _normalize
    d = re.sub(r"\bcod(igo)?\s*bar(ra)?[a-z]*\b", "", _normalize(descricao))
    return _tem_termo(d, _AMBIGUOUS_TERMS)


def tem_restaurante(descricao: str) -> bool:
    from app.categorizer import _tem_termo
    from app.utils import _normalize
    return familia_ambigua(descricao) is None and _tem_termo(_normalize(descricao), _TERMOS_RESTAURANTE)


def eh_cotidiano(valor: float | None, data: str | None, perfil=None) -> bool:
    """Refeição de rotina conforme o perfil do usuário (padrão: seg-qui, até R$ 90). Sem valor/data, não presume."""
    from app.rotina import Perfil
    perfil = perfil or Perfil()
    if valor is None or not data:
        return False
    try:
        from datetime import date
        dia = date.fromisoformat(str(data)[:10])
    except ValueError:
        return False
    return abs(float(valor)) <= perfil.limite and dia.weekday() in perfil.dias


def decidir_restaurante(merchant_key: str, valor: float | None, data: str | None, perfil=None) -> Decisao:
    """Rotina -> Alimentação automático. Senão pergunta (padrão Alimentação até responder)."""
    if eh_cotidiano(valor, data, perfil):
        return Decisao(*RESTAURANTE, "auto", CONF_REGRA, merchant_key, [RESTAURANTE])
    return Decisao(*RESTAURANTE, "revisar", CONF_DUVIDA, merchant_key, [RESTAURANTE, RESTAURANTE_LAZER])


async def classificar(user_phone: str, descricao: str,
                      fallback: tuple[str, str] | None = None,
                      valor: float | None = None, data: str | None = None) -> Decisao:
    """Orquestra: histórico do usuário → categorizador híbrido (modo melhor-esforço).
    `valor` e `data` (YYYY-MM-DD) só importam para restaurantes (rotina x lazer)."""
    from app.categorizer import categorizar_gasto_hibrido

    merchant_key = normalizar_merchant(descricao)
    if tem_restaurante(descricao):   # o mesmo lugar pode ser almoço ou jantar: histórico do lugar não decide
        from app.rotina import perfil as perfil_do_usuario
        return decidir_restaurante(merchant_key, valor, data, perfil_do_usuario(user_phone))
    familia = familia_ambigua(descricao)
    hist_familia = ler_historico(user_phone, chave_familia(familia)) if familia else Historico()
    hist_merchant = Historico() if familia else ler_historico(user_phone, merchant_key)

    # Atalho: com histórico consistente não gasta keyword/LLM
    d = decidir(("Outros", "Outros"), familia, hist_familia, hist_merchant, False, merchant_key)
    if d.status == "auto" and d.confidence == CONF_HISTORICO:
        return d

    base = await categorizar_gasto_hibrido(
        user_phone, descricao, fallback=fallback, permitir_melhor_esforco=True)
    return decidir(base, familia, hist_familia, hist_merchant,
                   tem_termo_ambiguo(descricao), merchant_key)
