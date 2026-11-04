"""Parser determinístico da fatura de cartão de crédito do Itaú (PDF, texto do pdfplumber com layout=True).

Lê só os lançamentos do período ("Lançamentos: compras e saques" e "Lançamentos: produtos e serviços"):
  - ignora "Pagamentos efetuados" e as compras parceladas das próximas faturas;
  - valor negativo (estorno/crédito) vira `income`;
  - a cobrança (billing_date) é o dia 1 do mês do vencimento impresso na fatura;
  - confere a soma com o "Total dos lançamentos atuais"; se não bater, devolve None (o chamador usa a leitura com IA).

Interface igual à dos outros parsers: parse(texto) -> {"bank_detected": "itau", "transactions": [dict, ...]} ou None.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date

logger = logging.getLogger(__name__)

_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
          "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
_RE_LINHA = re.compile(rf"^\s*(\d{{2}})/(\d{{2}})\s+(.+?)\s{{2,}}({_VALOR})(?=\s|$)")
_RE_PARCELA = re.compile(r"\s(\d{2})/(\d{2})\s*$")
_INICIO = ("lancamentos: compras e saques", "lancamentos: produtos e servicos", "lancamentos internacionais")
_FIM = ("total dos lancamentos atuais", "compras parceladas", "proximas faturas", "limites de credito", "limite total de credito")


def _norm(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t or "") if unicodedata.category(c) != "Mn").lower()


def eh_fatura(texto: str | None) -> bool:
    """Fatura de cartão do Itaú (e não extrato nem fatura de outro banco que cite o Itaú num pagamento)."""
    t = _norm(texto or "")
    return ("itau" in t and "lancamentos: compras e saques" in t and "total dos lancamentos atuais" in t)


def _valor(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def vencimento(texto: str, hoje: date | None = None) -> date | None:
    t = _norm(texto)
    m = re.search(r"vencimento\D{0,15}?(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    m = re.search(r"vencimento\W{0,12}(\d{1,2})\s+de\s+([a-z]+)", t)
    if m and m.group(2) in _MESES:
        hoje = hoje or date.today()
        mes = _MESES[m.group(2)]
        ano = hoje.year - 1 if mes - hoje.month > 6 else (hoje.year + 1 if hoje.month - mes > 6 else hoje.year)
        try:
            return date(ano, mes, int(m.group(1)))
        except ValueError:
            return None
    return None


def parse(texto: str, hoje: date | None = None) -> dict | None:
    venc = vencimento(texto, hoje)
    if venc is None:
        logger.warning("Itaú: não achei o vencimento da fatura; deixo para a leitura com IA.")
        return None
    billing = date(venc.year, venc.month, 1)
    txs: list[dict] = []
    dentro = False
    total_pdf = None
    for linha in texto.splitlines():
        n = _norm(linha)
        if "total dos lancamentos atuais" in n:
            m = re.search(rf"atuais\D*?({_VALOR})", n)
            if m and total_pdf is None:
                total_pdf = _valor(m.group(1))
        if any(k in n for k in _FIM):
            dentro = False
            continue
        if any(k in n for k in _INICIO):
            dentro = True
            continue
        if not dentro:
            continue
        m = _RE_LINHA.match(linha)
        if not m:
            continue
        dia, mes = int(m.group(1)), int(m.group(2))
        if not 1 <= mes <= 12:
            continue
        valor = _valor(m.group(4))
        desc = re.sub(r"\s{2,}", " ", m.group(3)).strip()
        parc_de = parc_total = None
        mp = _RE_PARCELA.search(" " + desc)
        if mp and 1 <= int(mp.group(1)) <= int(mp.group(2)) <= 99:
            parc_de, parc_total = int(mp.group(1)), int(mp.group(2))
            desc = re.sub(r"\s*\d{2}/\d{2}\s*$", "", desc).strip()
        ano = billing.year - 1 if mes > billing.month else billing.year
        try:
            data = date(ano, mes, dia)
        except ValueError:
            continue
        txs.append({"date": data.isoformat(), "description": desc, "amount": abs(valor),
                    "type": "income" if valor < 0 else "expense", "payment_method": "credito",
                    "installment_of": parc_de, "installment_total": parc_total,
                    "billing_date": billing.isoformat()})
    if not txs:
        return None
    if total_pdf is not None:
        soma = round(sum(t["amount"] if t["type"] == "expense" else -t["amount"] for t in txs), 2)
        if abs(soma - total_pdf) > 0.01:
            logger.warning(f"Itaú: a soma dos lançamentos ({soma:.2f}) difere do total da fatura ({total_pdf:.2f}); "
                           "deixo para a leitura com IA.")
            return None
    return {"bank_detected": "itau", "transactions": txs}
