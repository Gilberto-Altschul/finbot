# app/categorizer.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Categorizer — Motor Híbrido de Subcategorias e Inteligência de Merchants
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import app.database as db
import re
from app.llm import call_llm
from app.utils import _normalize

logger = logging.getLogger(__name__)

# Termos que SEMPRE devem disparar a pergunta ao usuário, independente de histórico ou banco.
_AMBIGUOUS_TERMS = [
    "restaurante", "restaurant", "bar", "pub", "boteco", "cervejaria",
    "padaria", "cafe", "café", "lanche", "bakery", "bistro", "gastronomia", "doceria"
]

# Termos que forçam a categoria Saúde para evitar conflitos com 'Seguro' no Financeiro
_HEALTH_FORCE_TERMS = [
    "plano de saude", "convenio", "unimed", "bradesco saude", "sulamerica", "amil", "odontoprev", "hapvida"
]

SYSTEM_CATEGORIZER = """
Você é o motor de classificação interna do FinBot. Sua única tarefa é ler a descrição de um gasto e mapeá-lo para uma SUBCATEGORIA e CATEGORIA válidas do sistema.

SUBCATEGORIAS E CATEGORIAS PERMITIDAS NO SISTEMA:
- Alimentação: Delivery, Mercado, Restaurante, Padaria, Lanche, Café
- Transporte: Aplicativo, Combustível, Estacionamento, Ônibus, Metrô, Oficina
- Moradia: Aluguel, Condomínio, Contas, Faxina, Manutenção, Utensílios
- Saúde: Farmácia, Academia, Médico, Dentista, Suplemento, Exame, Plano de Saúde, Convênio (Planos de saúde DEVEM ser categorizados aqui, NUNCA em Financeiro)
- Pets: Ração, Veterinário, Petshop, Banho
- Lazer: Streaming, Cinema, Show, Viagem, Bar, Balada, Presente
- Educação: Curso, Livro, Faculdade, Software
- Vestuário e Beleza: Roupa, Calçado, Cabelo, Barbearia, Manicure, Estética
- Financeiro: Seguro (Apenas Auto, Vida ou Residencial. NÃO inclua Plano de Saúde aqui), Tarifa, Anuidade, Imposto, Taxa

IMPORTANTE: Se a descrição contiver termos como 'Restaurante', 'Bar', 'Café', 'Padaria', 'Pub' ou 'Lanche', você DEVE retornar obrigatoriamente:
{"categoria": "Perguntar", "subcategoria": "Perguntar"}

Responda EXCLUSIVAMENTE com um JSON no formato estrito:
{"categoria": "Nome da Categoria", "subcategoria": "Nome da Subcategoria"}
"""

async def categorizar_gasto_hibrido(user_phone: str, descricao: str, fallback: tuple[str, str] | None = None) -> tuple[str, str]:
    """
    Fluxo de 4 Camadas para Precisão Máxima:
    0. Filtro de Ambiguidade (Restaurante/Bar/etc) -> Força Pergunta
    1. Regra do Usuário (Merchant personalizado já aprendido)
    2. Keywords Globais populadas no Banco de Dados
    3. LLM infere a Subcategoria -> Descobre a Categoria Mãe -> Salva Aprendizado
    """
    desc_norm = _normalize(descricao)

    # Camada -1: Proteção Hardcoded para Saúde (Evita conflito com Financeiro/Seguros)
    if any(_normalize(term) in desc_norm for term in _HEALTH_FORCE_TERMS):
        logger.info(f"Camada -1 (Saúde Priority) detectada: '{descricao}'. Categorizando como Saúde.")
        return "Saúde", "Plano de Saúde"

    # Camada 0: Bloqueio de Ambiguidade (Prioridade Absoluta)
    if any(_normalize(term) in desc_norm for term in _AMBIGUOUS_TERMS):
        logger.info(f"Camada 0 (Ambiguidade) detectada: '{descricao}'. Forçando interrupção para pergunta.")
        return "Perguntar", "Perguntar"
    
    # Camada 1: Tem regra personalizada do usuário para esse Merchant?
    mapping = db.get_user_merchant_mapping(user_phone, desc_norm)
    if mapping:
        logger.info(f"Camada 1 (User Merchant) resolvida: {desc_norm} -> {mapping['category_name']}/{mapping['subcategory_name']}")
        return mapping["category_name"], mapping["subcategory_name"]

    # Camada 2: Alguma keyword global de subcategoria mapeia com o texto?
    try:
        from app.database import get_db
        res = get_db().table("finbot_subcategories").select("category_name, name, keywords").execute()
        
        # Ordenamos para que 'Saúde' seja verificado antes de 'Financeiro' para evitar conflitos com 'Seguro'
        sorted_subs = sorted(res.data, key=lambda x: 0 if _normalize(x.get('category_name')) == "saude" else 1)
        
        for sub in sorted_subs:
            keywords = sub.get("keywords", [])
            if any(_normalize(kw) in desc_norm for kw in keywords):
                logger.info(f"Camada 2 (Global Subcategory) resolvida: {desc_norm} -> {sub['category_name']}/{sub['name']}")
                db.save_user_merchant_mapping(user_phone, desc_norm, sub["category_name"], sub["name"])
                return sub["category_name"], sub["name"]
    except Exception as e:
        logger.error(f"Erro na Camada 2 de categorização: {e}")

    # Camada 2.5: Se já temos uma sugestão da extração inicial (PDF), usamos ela para economizar IA
    if fallback and fallback[0] != "Outros":
        logger.info(f"Camada 2.5 (PDF Fallback) utilizada: {descricao} -> {fallback[0]}/{fallback[1]}")
        return fallback[0], fallback[1]

    # Camada 3: LLM acionada para decifrar o local inédito
    logger.info(f"Camada 3 (LLM) acionada para local inédito: {descricao}")
    try:
        response = await call_llm(
            system=SYSTEM_CATEGORIZER,
            history=[],
            message=f"Classifique a descrição: '{descricao}'",
            tools=[]
        )
        content = response.get("content", "").strip()
        if not content:
            raise ValueError("Resposta da LLM veio vazia.")

        # Sanitização: extrai apenas o conteúdo entre as primeiras e últimas chaves {}
        # Isso evita que o json.loads quebre se a IA mandar blocos de código markdown
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            content = match.group(0)
            
        data = json.loads(content)
        cat = data.get("categoria", "Outros")
        sub = data.get("subcategoria", "Outros")
        
        if cat not in ["Outros", "Perguntar"] and sub not in ["Outros", "Perguntar"]:
            db.save_user_merchant_mapping(user_phone, descricao, cat, sub)
            
        return cat, sub
    except Exception as e:
        logger.error(f"Erro na Camada 3 (LLM): {e}")
        return "Outros", "Outros"