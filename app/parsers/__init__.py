# app/parsers/__init__.py
"""
Parsers determinísticos por banco/tipo de documento.
"""

from app.parsers.base import detectar_banco_e_tipo
from app.parsers import santander_credito
from app.parsers import c6_credito

__all__ = ['detectar_banco_e_tipo', 'santander_credito', 'c6_credito']
