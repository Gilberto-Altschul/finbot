# app/parsers/santander_credito.py
"""Parser determinístico para fatura de cartão de crédito Santander.

IMPORTANTE: Santander usa duas colunas lado a lado nas páginas de despesas.
texto linear (layout=True) MISTURA as colunas incorretamente.
Por isso este parser precisa do objeto pdf (pdfplumber) para usar extract_words
com separação por posição X, não apenas o texto já extraído.
"""

import re
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

IGNORAR_DESC = [
    'pagamento de fatura', 'pagto fatura', 'valor total',
    'anuidade diferenciada', 'compra data descri',
    'parcela r$', 'saldo anterior', 'total despesas',
    'total de pagamentos', 'total de creditos', 'saldo desta fatura',
    'compras parceladas', 'compra data', 'descricao', 'azul seguros',
]

IGNORAR_SECAO = [
    'pagamento e demais', 'resumo da fatura', 'saldo total',
    'juros e custo', 'detalhamento', 'esfera', 'santander',
    'orientacoes', 'beneficiaria',
]

TX_RE = re.compile(
    r'(?:^|\s)'
    r'(\d{2}/\d{2})\s+'
    r'([A-Z][A-Z0-9 \*\-\./]+?)\s+'
    r'(?:(\d{2}/\d{2})\s+)?'
    r'(-?[\d\.]+,\d{2})'
    r'(?:\s|$)'
)


def _inferir_ano(mes_compra: int, mes_fatura: int, ano_fatura: int) -> int:
    return ano_fatura - 1 if mes_compra > mes_fatura else ano_fatura


def _detectar_vencimento(texto_pagina1: str) -> tuple[int, int, str] | None:
    """Detecta (mes_fatura, ano_fatura, billing_date) a partir do texto da página 1."""
    billing_date = None

    # Padrão Santander: "R$ X.XXX,XX DD/MM/YYYY R$XX.XXX,XX" (Total a Pagar | Vencimento | Limite)
    m_venc = re.search(r'R\$\s*[\d\.]+,\d{2}\s+(\d{2})/(\d{2})/(\d{4})\s+R\$', texto_pagina1)
    if m_venc:
        billing_date = f"{m_venc.group(3)}-{m_venc.group(2)}-{m_venc.group(1)}"
        fatura_mes = int(m_venc.group(2))
        fatura_ano = int(m_venc.group(3))
        return fatura_mes, fatura_ano, billing_date

    # Fallback 1: período de compras "Esta Fatura DD/MM/YY a DD/MM/YY" (fim do período, não vencimento real)
    m = re.search(r'Esta Fatura\s+\d{2}/\d{2}/\d{2}\s+a\s+(\d{2})/(\d{2})/(\d{2})', texto_pagina1, re.IGNORECASE)
    if m:
        fatura_mes = int(m.group(2))
        fatura_ano = 2000 + int(m.group(3))
        billing_date = f"{fatura_ano}-{fatura_mes:02d}-01"
        return fatura_mes, fatura_ano, billing_date

    # Fallback 2: qualquer "Vencimento ... DD/MM/YYYY"
    m_venc2 = re.search(r'Vencimento\D{0,15}?(\d{2})/(\d{2})/(\d{4})', texto_pagina1, re.IGNORECASE)
    if m_venc2:
        billing_date = f"{m_venc2.group(3)}-{m_venc2.group(2)}-{m_venc2.group(1)}"
        fatura_mes = int(m_venc2.group(2))
        fatura_ano = int(m_venc2.group(3))
        return fatura_mes, fatura_ano, billing_date

    return None


def parse_from_pdfplumber(pdf) -> dict:
    """
    Recebe um objeto pdfplumber.PDF já aberto e extrai transações usando
    separação de colunas por posição X (necessário para o layout do Santander).
    """
    p1_text = pdf.pages[0].extract_text() or ""
    vencimento = _detectar_vencimento(p1_text)

    if vencimento:
        fatura_mes, fatura_ano, billing_date = vencimento
        logger.info(f"[Santander Parser] Fatura: {fatura_mes:02d}/{fatura_ano}, vencimento: {billing_date}")
    else:
        now = datetime.now()
        fatura_mes, fatura_ano = now.month, now.year
        billing_date = now.strftime("%Y-%m-%d")
        logger.warning(f"[Santander Parser] Vencimento não detectado. Fallback: {billing_date}")

    transactions = []
    seen = set()

    for page in pdf.pages:
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        if not words:
            continue

        col_split = page.width / 2
        left_by_y = defaultdict(list)
        right_by_y = defaultdict(list)

        for w in words:
            y_key = round(w['top'] / 3) * 3
            if w['x0'] < col_split:
                left_by_y[y_key].append(w)
            else:
                right_by_y[y_key].append(w)

        def process_column(col_dict):
            col_txs = []
            for y in sorted(col_dict.keys()):
                ws = sorted(col_dict[y], key=lambda w: w['x0'])
                line = ' '.join(w['text'] for w in ws)
                line_lower = line.lower()

                if any(ign in line_lower for ign in IGNORAR_SECAO + IGNORAR_DESC):
                    continue

                for m in TX_RE.finditer(line):
                    data_str = m.group(1)
                    desc = m.group(2).strip()
                    parcela_str = m.group(3)
                    valor_str = m.group(4)

                    try:
                        dia, mes = int(data_str[:2]), int(data_str[3:])
                        if dia > 31 or mes > 12:
                            continue
                        ano = _inferir_ano(mes, fatura_mes, fatura_ano)
                        date_iso = f"{ano}-{mes:02d}-{dia:02d}"
                    except Exception:
                        continue

                    try:
                        valor = float(valor_str.replace('.', '').replace(',', '.'))
                    except Exception:
                        continue

                    if valor <= 0.50:
                        continue

                    installment_of = installment_total = None
                    if parcela_str:
                        try:
                            installment_of = int(parcela_str[:2])
                            installment_total = int(parcela_str[3:])
                        except Exception:
                            pass

                    desc_lower = desc.lower()
                    if any(ign in desc_lower for ign in IGNORAR_DESC):
                        continue
                    if len(desc) < 3:
                        continue

                    key = (date_iso, desc[:25], f"{valor:.2f}", str(installment_of))
                    if key in seen:
                        continue
                    seen.add(key)

                    col_txs.append({
                        'date': date_iso,
                        'description': desc,
                        'amount': valor,
                        'installment_of': installment_of,
                        'installment_total': installment_total,
                        'payment_method': 'credito',
                        'type': 'expense',
                        'billing_date': billing_date,
                    })
            return col_txs

        transactions.extend(process_column(left_by_y))
        transactions.extend(process_column(right_by_y))

    logger.info(f"[Santander Parser] Extraídas {len(transactions)} transações.")
    return {
        'transactions': transactions,
        'billing_date': billing_date,
        'bank_detected': 'santander',
    }


def parse(texto: str) -> dict:
    """
    Fallback simplificado para quando só temos o texto (sem objeto pdf).
    AVISO: não separa colunas — pode perder transações no Santander.
    Prefira parse_from_pdfplumber quando possível.
    """
    logger.warning("[Santander Parser] Usando parse() sem column-splitting — pode perder transações.")
    vencimento = _detectar_vencimento(texto)
    if vencimento:
        fatura_mes, fatura_ano, billing_date = vencimento
    else:
        now = datetime.now()
        fatura_mes, fatura_ano = now.month, now.year
        billing_date = now.strftime("%Y-%m-%d")

    transactions = []
    seen = set()
    secao_atual = None

    for line in texto.split('\n'):
        line_lower = line.strip().lower()

        if re.match(r'^parcelamentos\s*$', line_lower):
            secao_atual = 'parcelamentos'
            continue
        elif re.match(r'^despesas\s*$', line_lower):
            secao_atual = 'despesas'
            continue
        elif re.match(r'^pagamento e demais cr', line_lower):
            secao_atual = 'pagamentos'
            continue
        elif re.match(r'^resumo da fatura', line_lower):
            secao_atual = None
            continue

        if secao_atual not in ('parcelamentos', 'despesas'):
            continue
        if any(ign in line_lower for ign in IGNORAR_SECAO + IGNORAR_DESC):
            continue

        for m in TX_RE.finditer(line.strip()):
            data_str, desc, parcela_str, valor_str = m.groups()
            desc = desc.strip()
            desc_lower = desc.lower()

            if any(ign in desc_lower for ign in IGNORAR_DESC) or len(desc) < 3:
                continue

            try:
                dia, mes = int(data_str[:2]), int(data_str[3:])
                if dia > 31 or mes > 12:
                    continue
                ano = _inferir_ano(mes, fatura_mes, fatura_ano)
                date_iso = f"{ano}-{mes:02d}-{dia:02d}"
            except Exception:
                continue

            try:
                valor = float(valor_str.replace('.', '').replace(',', '.'))
            except Exception:
                continue
            if valor <= 0.50:
                continue

            installment_of = installment_total = None
            if parcela_str:
                try:
                    installment_of = int(parcela_str[:2])
                    installment_total = int(parcela_str[3:])
                except Exception:
                    pass

            key = (date_iso, desc[:25], f"{valor:.2f}", str(installment_of))
            if key in seen:
                continue
            seen.add(key)

            transactions.append({
                'date': date_iso,
                'description': desc,
                'amount': valor,
                'installment_of': installment_of,
                'installment_total': installment_total,
                'payment_method': 'credito',
                'type': 'expense',
                'billing_date': billing_date,
            })

    return {
        'transactions': transactions,
        'billing_date': billing_date,
        'bank_detected': 'santander',
    }
