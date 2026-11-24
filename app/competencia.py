# app/competencia.py
# Competência da receita: o salário que cai nos últimos dias do mês passa a contar no mês SEGUINTE nos relatórios.
#
# O que muda é só `billing_date` (a data de competência, a mesma que o cartão usa para a fatura). A data real da entrada
# (`purchase_date`) continua igual, então o Fluxo (que detecta entradas pela data real) e o extrato não mudam.
#
# Configuração (variável de ambiente, sem SQL; só vale para quem está listado):
#   FINFLOW_RECEITA_CORTE=0373fa8f1a099c9c:28
#   (chave do usuário = os 16 caracteres do diag_resumo/diag_painel; vários separados por vírgula ou ponto e vírgula)
#   Receita de "Salário e Pró-labore" recebida a partir do dia 28 conta no dia 1º do mês seguinte.
#   Dia maior que o último do mês vale o último dia (ex.: 31 = "último dia do mês"). Para desfazer: apague a variável.
import os
import re
from calendar import monthrange
from datetime import date

SUBCATEGORIA = "salario e pro labore"   # comparada sem acento/caixa/pontuação


def _plano(texto: str) -> str:
    from app.utils import _normalize
    return re.sub(r"[^a-z0-9]+", " ", _normalize(texto or "")).strip()


def _cortes() -> dict[str, int]:
    """{chave do usuário: dia de corte}. Ignora entradas inválidas (dia fora de 2..31, chave vazia)."""
    saida: dict[str, int] = {}
    for item in re.split(r"[;,]", os.environ.get("FINFLOW_RECEITA_CORTE", "") or ""):
        if ":" not in item:
            continue
        chave, dia = (x.strip() for x in item.split(":", 1))
        if chave and dia.isdigit() and 2 <= int(dia) <= 31:
            saida[chave.lower()] = int(dia)
    return saida


def corte_de(user_phone: str) -> int | None:
    """Dia de corte configurado para este usuário, ou None."""
    cortes = _cortes()
    if not cortes:
        return None
    from app.utils import get_lookup_prefix
    return cortes.get(get_lookup_prefix(user_phone).lower())


def competencia(data_iso: str, corte: int) -> str | None:
    """Competência (1º do mês seguinte) se a data cai a partir do corte; senão None."""
    try:
        d = date.fromisoformat(str(data_iso)[:10])
    except (TypeError, ValueError):
        return None
    if d.day < min(corte, monthrange(d.year, d.month)[1]):
        return None
    return date(d.year + d.month // 12, d.month % 12 + 1, 1).isoformat()


def billing_receita(user_phone: str, subcategoria: str | None, data_iso: str, billing_atual: str) -> str:
    """Competência final de uma receita que NÃO é crédito de cartão: `billing_atual` se a regra não se aplica."""
    try:
        if _plano(subcategoria or "") != SUBCATEGORIA:
            return billing_atual
        corte = corte_de(user_phone)
        if not corte:
            return billing_atual
        return competencia(data_iso, corte) or billing_atual
    except Exception:
        return billing_atual   # a regra nunca derruba a importação
