# app/utils.py
import unicodedata

def _fmt(value: float) -> str:
    """Formata valores para o padrão R$ 0,00"""
    if value is None: return "0,00"
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def formatar_data(data_str: str) -> str:
    """Converte data YYYY-MM-DD para DD/MM"""
    return f"{data_str[8:10]}/{data_str[5:7]}"

def _normalize(text: str) -> str:
    if not text: return ""
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").lower().strip()