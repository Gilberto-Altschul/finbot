"""Nome do banco a partir do que a Pluggy já informa — sem o usuário digitar nada.

No MeuPluggy o conector aparece só como "MeuPluggy", mas a conta corrente traz
`bankData.transferNumber` no formato "341/9157/00031067-4" (banco/agência/conta).
O primeiro bloco é o código de compensação do banco. O cartão herda o nome da conta
do MESMO item. Se o código não vier (ou não estiver na tabela), cai no nome que a
Pluggy deu à conta e, por fim, no apelido que o usuário definir.
"""
from __future__ import annotations

import re

CODIGOS = {
    "001": "Banco do Brasil", "033": "Santander", "041": "Banrisul", "077": "Inter", "104": "Caixa",
    "121": "Agibank", "208": "BTG Pactual", "212": "Original", "237": "Bradesco", "260": "Nubank",
    "280": "Will Bank", "290": "PagBank", "318": "BMG", "323": "Mercado Pago", "336": "C6 Bank",
    "341": "Itaú", "348": "XP", "380": "PicPay", "422": "Safra", "623": "Pan", "707": "Daycoval",
    "735": "Neon", "746": "Modal", "748": "Sicredi", "756": "Sicoob",
}


def codigo_do_banco(transfer_number: str | None) -> str | None:
    """'341/9157/00031067-4' -> '341'. Só aceita o formato banco/agência/conta (3 blocos)."""
    if not transfer_number:
        return None
    partes = str(transfer_number).split("/")
    if len(partes) == 3 and re.fullmatch(r"\d{1,4}", partes[0].strip()):
        return partes[0].strip().zfill(3)
    return None


def nome_do_banco(codigo: str | None) -> str | None:
    return CODIGOS.get(codigo) if codigo else None


def _rotulo(tipo: str) -> str:
    return "cartão" if tipo == "CREDIT" else "conta"


def aplicar_nomes(contas: list[dict]) -> list[dict]:
    """
    Preenche `banco` e `nome` de cada conta (muda a lista no lugar e a devolve).
    Espera, por conta: item_id, tipo, nome (o da Pluggy) e, nas contas correntes, banco_codigo.
    """
    por_item: dict[str, str] = {}
    for c in contas:
        nome = nome_do_banco(c.get("banco_codigo"))
        if nome and c.get("item_id"):
            por_item.setdefault(c["item_id"], nome)
    for c in contas:
        banco = por_item.get(c.get("item_id"))
        c["banco"] = banco
        if banco:
            c["nome"] = f"{banco} {_rotulo(c['tipo'])}"
    # Duas contas com o mesmo nome (ex.: dois cartões do mesmo banco) precisam se distinguir.
    vistos: dict[tuple, int] = {}
    for c in contas:
        chave = (c["tipo"], c["nome"])
        vistos[chave] = vistos.get(chave, 0) + 1
    contadas: dict[tuple, int] = {}
    for c in contas:
        chave = (c["tipo"], c["nome"])
        if vistos[chave] > 1:
            contadas[chave] = contadas.get(chave, 0) + 1
            final = (c.get("numero") or "")[-4:]
            c["nome"] = f"{c['nome']} ({'final ' + final if final else contadas[chave]})"
    return contas


def resolver_conta(contas: list[dict], texto: str, normalizar) -> tuple[list[dict], str]:
    """
    Acha a(s) conta(s) que o usuário quis dizer em 'nubank', 'itau cartao'...
    Retorna (casos, motivo) com motivo = 'ok' | 'nenhuma' | 'varias'.
    Casa por substring nos dois sentidos, sem acento/maiúscula, e ignora as palavras genéricas.
    """
    generico = {"cartao", "conta", "de", "do", "da", "o", "a"}
    q = [p for p in normalizar(texto).split() if p not in generico]
    if not q:
        return contas, "ok"
    achadas = [c for c in contas if all(p in normalizar(c["nome"]) for p in q)]
    if not achadas:
        return [], "nenhuma"
    return achadas, "ok" if len(achadas) == 1 else "varias"
