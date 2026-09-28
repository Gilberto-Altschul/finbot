# app/utils.py
import unicodedata
import hashlib
import hmac
from datetime import date
from cryptography.fernet import Fernet
from app.config import get_settings

SISTEMA_CATEGORIAS = ["Moradia", "Alimentação", "Transporte", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets", "Empresa", "Família e Dependentes", "Receitas"]

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

def _get_cipher():
    """Recupera o objeto Fernet usando a chave do .env."""
    key = get_settings().encryption_key
    if not key:
        # Avisar no log que a criptografia está usando uma chave volátil (apenas para dev)
        return Fernet(Fernet.generate_key())
    return Fernet(key.encode())

def criptografar_telefone(telefone: str) -> str:
    """Criptografa o telefone com token Fernet e prefixo determinístico para buscas."""
    if not telefone: return ""
    cipher = _get_cipher()
    # Prefixo determinístico estável para permitir buscas (Indexable)
    h = hmac.new(get_settings().encryption_key.encode(), telefone.encode(), hashlib.sha256).hexdigest()[:16]
    token = cipher.encrypt(telefone.encode()).decode()
    return f"{h}:{token}"

def descriptografar_telefone(encrypted_str: str) -> str:
    """Extrai o telefone original do formato HASH:TOKEN."""
    if not encrypted_str or ":" not in encrypted_str: return encrypted_str
    token = encrypted_str.split(":")[1]
    return _get_cipher().decrypt(token.encode()).decode()

def get_lookup_prefix(telefone: str) -> str:
    """Retorna o prefixo estável do telefone para filtros no banco."""
    return hmac.new(get_settings().encryption_key.encode(), telefone.encode(), hashlib.sha256).hexdigest()[:16]

def parse_mes_fatura(input_mes: str | int) -> str:
    """
    Converte um mês informado (número ou nome) em uma string 'YYYY-MM-01',
    gerenciando automaticamente o ano atual e a virada de ano (ex: Dez -> Jan).
    """
    hoje = date.today()
    ano = hoje.year
    mes_atual = hoje.month

    meses_map = {
        'janeiro': 1, 'fevereiro': 2, 'março': 3, 'abril': 4, 'maio': 5, 'junho': 6,
        'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12
    }
    
    if isinstance(input_mes, str):
        input_lower = input_mes.strip().lower()
        if input_lower in meses_map:
            mes = meses_map[input_lower]
        else:
            try:
                mes = int(input_lower)
            except ValueError:
                raise ValueError(f"Mês inválido: {input_mes}")
    else:
        mes = int(input_mes)

    if not (1 <= mes <= 12):
        raise ValueError(f"Mês fora do intervalo (1-12): {mes}")

    # Lógica de virada de ano: se estamos no fim do ano e pedem um mês inicial, ou vice-versa
    if mes < mes_atual and (mes_atual - mes) > 6:
        ano += 1
    elif mes > mes_atual and (mes - mes_atual) > 6:
        ano -= 1

    return date(ano, mes, 1).isoformat()