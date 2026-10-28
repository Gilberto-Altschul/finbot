# app/classificador.py
# Decide COMO uma transação entra no banco: confirmada, automática ou "a revisar".
# Nunca bloqueia a importação esperando o usuário.
import re
from dataclasses import dataclass, field

from app.merchant import normalizar_merchant, familia_ambigua
from app.taxonomia import A_CLASSIFICAR, COMPRAS_ONLINE
from app.merchant_stats import (
    Historico, e_consistente, ler_historico, chave_familia, MIN_AMOSTRAS, MIN_AMOSTRAS_FAMILIA,
)

CONF_HISTORICO = 0.95   # usuário respondeu o mesmo, de forma consistente
CONF_REGRA = 0.75       # keyword/LLM sem ambiguidade
CONF_DUVIDA = 0.5       # termo ambíguo ou histórico dividido
CONF_NENHUMA = 0.0      # Outros / sem palpite útil


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
    if extra and extra not in out and extra != A_CLASSIFICAR:
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

    if familia:      # marketplace sem histórico: nunca confiar no palpite da loja; vai para Compras Online (sem pergunta)
        return Decisao(*COMPRAS_ONLINE, "auto", CONF_REGRA, chave, [COMPRAS_ONLINE])
    if base == A_CLASSIFICAR:   # dúvida real: pergunta ao usuário (a taxonomia v2 não chuta)
        return Decisao(*A_CLASSIFICAR, "revisar", CONF_NENHUMA, chave, [])
    # Comer fora é sempre Alimentação: não pergunta.
    return Decisao(base[0], base[1], "auto", CONF_REGRA, chave, [base])


def tem_termo_ambiguo(descricao: str) -> bool:
    """Marketplace (Mercado Livre, Amazon...): o palpite existe, mas a loja vende de tudo.
    Usa as mesmas regras do categorizer sem exigir nenhuma função nova nele."""
    from app.categorizer import _tem_termo, _AMBIGUOUS_TERMS
    from app.utils import _normalize
    d = re.sub(r"\bcod(igo)?\s*bar(ra)?[a-z]*\b", "", _normalize(descricao))
    return _tem_termo(d, _AMBIGUOUS_TERMS)


async def classificar(user_phone: str, descricao: str,
                      fallback: tuple[str, str] | None = None,
                      valor: float | None = None, data: str | None = None) -> Decisao:
    """Orquestra: histórico do usuário → categorizador híbrido (modo melhor-esforço).
    `valor` e `data` ficam na assinatura por compatibilidade (antes serviam à pergunta rotina x lazer, que deixou de existir)."""
    from app.categorizer import categorizar_gasto_hibrido

    merchant_key = normalizar_merchant(descricao)
    familia = familia_ambigua(descricao)
    hist_familia = ler_historico(user_phone, chave_familia(familia)) if familia else Historico()
    hist_merchant = Historico() if familia else ler_historico(user_phone, merchant_key)

    # Atalho: com histórico consistente não gasta keyword/LLM
    d = decidir(A_CLASSIFICAR, familia, hist_familia, hist_merchant, False, merchant_key)
    if d.status == "auto" and d.confidence == CONF_HISTORICO:
        return d

    from app.categorizer import ORIGEM_PALPITE, CERTEZA_IA
    base = await categorizar_gasto_hibrido(
        user_phone, descricao, fallback=fallback, permitir_melhor_esforco=True)
    d = decidir(base, familia, hist_familia, hist_merchant,
                tem_termo_ambiguo(descricao), merchant_key)
    if ORIGEM_PALPITE.get() == "llm" and d.status == "auto":
        # Lugar inédito classificado só por chute da IA (ex.: "Salute Prime" -> Saúde): pergunta antes de valer.
        return Decisao(d.categoria, d.subcategoria, "revisar", CONF_DUVIDA, d.merchant_key, [(d.categoria, d.subcategoria)])
    certeza_ia = CERTEZA_IA.get()
    if certeza_ia is not None and d.status == "auto":
        # A IA decidiu sozinha com mais de 50% de certeza: grava a certeza dela, para dar para achar e conferir depois.
        d = Decisao(d.categoria, d.subcategoria, d.status, certeza_ia, d.merchant_key, d.sugestoes)
    return d


# ── Receitas: categoria-pai sempre "Receitas", sem IA e sem pergunta ─────────
_REGRAS_RECEITA = [
    ("Salário e Pró-labore", ("salario", "folha de pagamento", "proventos", "remuneracao", "pro labore", "prolabore")),
    ("Aposentadoria e Benefícios", ("inss", "aposentadoria", "beneficio", "pensao do inss")),
    ("Rendimentos", ("rendimento", "rendimentos", "juros", "dividendo", "dividendos", "jcp", "cdb", "resgate", "lci", "lca")),
    ("Reembolso e Estorno", ("estorno", "reembolso", "devolucao", "cashback", "reversao", "chargeback")),
    ("Freelance e Bônus", ("freela", "freelance", "bonus", "premio", "comissao")),
    ("Transferências de Terceiros", ("pix", "ted", "doc", "transferencia", "transf")),
]


def classificar_receita(descricao: str) -> Decisao:
    """Toda entrada fica em Receitas (nunca categoria de gasto, ex.: "Família e Dependentes").
    A subcategoria vem de palavras-chave simples; sem palavra, fica "Outras Receitas". Nunca pergunta, nunca chuta com IA.
    Transferência entre contas próprias e investimento NÃO são receita: o banco (movement) reclassifica na gravação."""
    from app.utils import _normalize
    palavras = set(_normalize(descricao or "").split())
    sub = "Outras Receitas"
    for nome, termos in _REGRAS_RECEITA:
        if any((t in palavras) if " " not in t else (t in _normalize(descricao or "")) for t in termos):
            sub = nome
            break
    return Decisao("Receitas", sub, "auto", CONF_REGRA, normalizar_merchant(descricao or ""), [])
