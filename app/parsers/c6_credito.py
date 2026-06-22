# app/parsers/c6_credito.py
"""Parser determinístico para fatura de cartão de crédito C6 Bank."""

import re
import logging

logger = logging.getLogger(__name__)

MESES_PT = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}

IGNORAR_DESC = [
    'pag fatura', 'estorno tarifa', 'estorno', 'pagamento de fatura',
]

# Formato C6: "DD mmm  DESCRIÇÃO [- Parcela X/Y]  VALOR"
LINE_RE = re.compile(
    r'^\s*(\d{2})\s+([a-zç]{3})\s+'           # data: DD mmm
    r'(.+?)'                                    # descrição
    r'(?:\s*-\s*(?:Parcela\s+(\d+)/(\d+)|Estorno))?'  # parcela ou estorno opcional
    r'\s+(-?[\d\.]+,\d{2})\s*$',                # valor
    re.IGNORECASE
)


def _inferir_ano(mes_compra: int, mes_fatura: int, ano_fatura: int) -> int:
    return ano_fatura - 1 if mes_compra > mes_fatura else ano_fatura


def _detectar_vencimento(texto: str) -> tuple[int, int, int] | None:
    """Detecta dia/mês/ano de vencimento. Retorna (dia, mes, ano) ou None."""
    # Padrão 1: "01/06/2026" (formato comum no rodapé/QR Pix)
    m = re.search(r'Vencimento:?\s*[\r\n]*\s*(\d{2})/(\d{2})/(\d{4})', texto, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    # Padrão 2: "Vencimento: 01 de Junho" (sem ano explícito — tenta achar ano em outro lugar)
    m = re.search(r'Vencimento:?\s*(\d{1,2})\s+de\s+([a-zç]+)', texto, re.IGNORECASE)
    if m:
        dia = int(m.group(1))
        mes_nome = m.group(2).lower()[:3]
        mes = MESES_PT.get(mes_nome)
        # Procura ano em qualquer DD/MM/YYYY próximo
        m_ano = re.search(r'\d{2}/\d{2}/(\d{4})', texto)
        ano = int(m_ano.group(1)) if m_ano else None
        if mes and ano:
            return dia, mes, ano

    return None


def parse(texto: str) -> dict:
    """
    Parseia o texto extraído (via pdfplumber) de uma fatura C6 Bank.
    Retorna dict com 'transactions' e 'billing_date'.
    """
    vencimento = _detectar_vencimento(texto)
    if vencimento:
        fatura_dia, fatura_mes, fatura_ano = vencimento
        billing_date = f"{fatura_ano}-{fatura_mes:02d}-{fatura_dia:02d}"
        logger.info(f"[C6 Parser] Vencimento detectado: {billing_date}")
    else:
        from datetime import datetime
        now = datetime.now()
        fatura_mes, fatura_ano = now.month, now.year
        billing_date = now.strftime("%Y-%m-%d")
        logger.warning(f"[C6 Parser] Vencimento não detectado. Usando fallback: {billing_date}")

    transactions = []
    seen = set()

    for raw_line in texto.split('\n'):
        line = raw_line.strip()
        if not line:
            continue

        m = LINE_RE.match(line)
        if not m:
            continue

        dia, mes_str, desc, parc_atual, parc_total, valor_str = m.groups()
        desc = desc.strip()

        # Remove informações extras de conversão de moeda/IOF que ficam coladas na descrição
        desc = re.sub(r'\s*(USD\s+[\d,]+\s*\|.*|IOF Transações Exterior.*)$', '', desc, flags=re.IGNORECASE).strip()

        line_lower = line.lower()
        desc_lower = desc.lower()

        if any(ign in desc_lower or ign in line_lower for ign in IGNORAR_DESC):
            continue

        mes = MESES_PT.get(mes_str.lower())
        if not mes:
            continue

        try:
            ano = _inferir_ano(mes, fatura_mes, fatura_ano)
            date_iso = f"{ano}-{mes:02d}-{int(dia):02d}"
        except Exception:
            continue

        try:
            valor = float(valor_str.replace('.', '').replace(',', '.'))
        except Exception:
            continue

        if valor <= 0:
            continue

        installment_of = int(parc_atual) if parc_atual else None
        installment_total = int(parc_total) if parc_total else None

        # Dedup local (mesma linha pode aparecer 2x por causa de layout)
        key = (date_iso, desc[:30], f"{valor:.2f}", str(installment_of))
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

    logger.info(f"[C6 Parser] Extraídas {len(transactions)} transações.")
    return {
        'transactions': transactions,
        'billing_date': billing_date,
        'bank_detected': 'c6',
    }
