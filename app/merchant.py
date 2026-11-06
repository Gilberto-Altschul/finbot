# app/merchant.py
# Normalização de estabelecimentos — funções PURAS (sem I/O), fáceis de testar.
import re
import unicodedata


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_PARCELA = re.compile(r"\bparc(?:ela)?\.?\s*\d{1,2}\s*(?:/|de)\s*\d{1,2}\b|\b\d{1,2}/\d{1,2}\b")
# Mês de cobrança no fim da descrição ("SCP ESSENCIAL- OUT/26"): muda todo mês e quebraria a chave do estabelecimento.
_MES_ANO = re.compile(r"\b(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\s*/\s*\d{2,4}\b")
_PROCESSADORA = re.compile(r"^(?:mp|pag|pagseguro|ec|iz|sumup|stone|cielo|getnet|rede)\s*\*\s*")
_PREFIXOS = re.compile(
    r"^(?:compra\s+(?:no\s+)?(?:debito|credito|cartao)|pix\s+(?:enviado|recebido|transf(?:erencia)?)|"
    r"transferencia\s+(?:enviada|recebida)|pag(?:amento)?\s+(?:de\s+)?(?:boleto|conta)|"
    r"debito\s+automatico|rscss|compra)\s+"
)
# Data DDMM colada no fim do nome, comum em débito de maquininha ("CRAZY FOR PE0511"): muda todo dia e quebraria a chave.
_DDMM_COLADO = re.compile(r"(?<=[a-z])(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])\b")
_LEGAIS = re.compile(r"\b(?:ltda|eireli|epp|me|sa|s a|cia)\b")
_NUM_LONGO = re.compile(r"\b\d{3,}\b")


def normalizar_merchant(descricao: str, max_tokens: int = 4) -> str:
    """Chave estável do estabelecimento: sem acento, prefixo de operação, sufixo legal, nº de loja/parcela."""
    base = sem_acento((descricao or "").lower()).strip()
    s = _MES_ANO.sub(" ", base)
    s = _PARCELA.sub(" ", s)
    s = _DDMM_COLADO.sub("", s)
    s = _PROCESSADORA.sub("", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(2):
        s = _PREFIXOS.sub("", s)
    s = _LEGAIS.sub(" ", s)
    s = _NUM_LONGO.sub(" ", s)
    chave = " ".join(s.split()[:max_tokens])
    return chave or re.sub(r"\s+", " ", base)[:30]


# Estabelecimentos que vendem "de tudo" ou escondem o vendedor real.
_FAMILIAS = {
    "mercadolivre": r"mercado\s?livre",
    "amazon": r"amazon|amzn",
    "shopee": r"shopee",
    "aliexpress": r"aliexpress",
    "magalu": r"magalu|magazine\s+luiza",
    "americanas": r"americanas|submarino",
    "mercadopago": r"mercado\s?pago",
    "paypal": r"paypal",
}


def familia_ambigua(descricao: str) -> str | None:
    s = re.sub(r"[^a-z0-9 ]+", " ", sem_acento((descricao or "").lower()))
    for nome, padrao in _FAMILIAS.items():
        if re.search(rf"(?<![a-z0-9])(?:{padrao})", s):
            return nome
    return None


def contem_termo(texto_norm: str, termo_norm: str, palavra_inteira: bool = False) -> bool:
    """Substring por padrão; palavra inteira para termos curtos/perigosos ('bar', 'cea', 'amil')."""
    if palavra_inteira:
        return re.search(rf"(?<![a-z0-9]){re.escape(termo_norm)}(?![a-z0-9])", texto_norm) is not None
    return termo_norm in texto_norm


# Beneficiário: o gasto é "de quem" (pet, filhos...). Códigos iguais aos da coluna finbot_expenses.beneficiary.
BENEFICIARIOS = {"self": "Você", "family": "Família", "children": "Filhos", "pet": "Pet", "home": "Casa"}
_SINONIMOS_BENEF = {
    "pet": ("pet", "pets", "bicho", "cachorro", "cao", "gato", "animal"),
    "children": ("filho", "filhos", "filha", "filhas", "crianca", "criancas", "neto"),
    "family": ("familia", "familiar"),
    "home": ("casa", "lar", "apartamento", "apto"),
    "self": ("eu", "voce", "mim", "meu", "pessoal", "self"),
}


def codigo_beneficiario(texto: str | None) -> str | None:
    """'Pet', 'pets', 'meu cachorro' -> 'pet'; texto desconhecido -> None."""
    t = sem_acento((texto or "").lower())
    palavras = re.findall(r"[a-z]+", t)
    for cod, sins in _SINONIMOS_BENEF.items():
        if any(p in sins for p in palavras):
            return cod
    return None


def beneficiario_aprendido(descricao: str, aprendidos: dict[str, str]) -> str | None:
    """Beneficiário que o usuário já ensinou para este estabelecimento (chave estável do merchant)."""
    return aprendidos.get(normalizar_merchant(descricao)) if aprendidos else None
