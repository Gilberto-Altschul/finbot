# app/categorizer.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Categorizer — Motor Híbrido de Subcategorias e Inteligência de Merchants
# ─────────────────────────────────────────────────────────────────────────────
import json
import logging
import app.database as db
import re
from app.llm import call_llm
from app.utils import _normalize, SISTEMA_CATEGORIAS

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

# Termos que forçam a categoria Transporte para Seguro Automóvel
_AUTO_FORCE_TERMS = [
    "seguro auto", "seguro automovel", "seguro veiculo", "porto seguro", "azul seguros", "tokio marine", "allianz"
]

# Termos que forçam a categoria Vestuário e Beleza para evitar 'Pessoal'
_CLOTHING_FORCE_TERMS = [
    "roupa", "vestuario", "calcado", "tenis", "sapato", "zara", "renner", "cea", "riachuelo", "shein", "loja de roupa", "lingerie",
    "manicure", "pedicure", "salao", "cabelo", "estetica", "barbearia", "beleza", "cosmetico"
]

# Termos que forçam a categoria Família e Dependentes
_FAMILY_FORCE_TERMS = [
    "apoio familiar", "apoio", "mesada", "pensao", "ajuda familiar", "familiares", "dependentes"
]

SYSTEM_CATEGORIZER = """
Você é o motor de classificação interna do FinBot. Sua única tarefa é ler a descrição de um gasto e mapeá-lo para uma SUBCATEGORIA e CATEGORIA válidas do sistema.

SUBCATEGORIAS E CATEGORIAS PERMITIDAS NO SISTEMA:
- Moradia: Aluguel/Financiamento, Condomínio, Energia, Água e Saneamento, Gás, Internet e TV, Empregada/Diarista, Reforma e Manutenção, Mobília, Seguro Residencial
- Alimentação: Mercado, Feira e Hortifruti, Delivery, Restaurante, Padaria e Café, Bar e Petisco
- Transporte: Aplicativo, Combustível, Transporte Público, Estacionamento, Manutenção Veículo, Financiamento Veículo, Passagem Aérea
- Saúde: Plano de Saúde, Farmácia, Consulta e Exame, Academia e Esportes, Terapia, Nutrição
- Lazer: Streaming, Cinema e Shows, Viagem, Bar e Balada, Hobbies e Jogos, Restaurante Social
- Vestuário e Beleza: Roupas, Calçados, Beleza e Cabelo, Cosméticos, Presentes
- Educação: Escola e Faculdade, Curso Online, Material Escolar, Idiomas
- Financeiro: Empréstimo e Parcela, Seguro, Investimento, Imposto, Tarifa Bancária, Apostas
- Pets: Ração e Petisco, Veterinário, Petshop e Banho, Plano Pet
- Família e Dependentes: Mesada, Pensão, Apoio Familiar, Presente Familiar, Emergência Familiar, Empréstimo Pessoal
- Empresa: MEI, Impostos PJ, Escritório, Marketing, Pró-labore, Ferramentas

ATENÇÃO: NUNCA use a categoria 'Pessoal'. Se o gasto parecer de uso pessoal, use 'Lazer' ou 'Vestuário e Beleza' conforme o contexto.

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

    # Camada -1: Proteção Hardcoded para Família e Dependentes (Prioridade Máxima)
    if any(_normalize(term) in desc_norm for term in _FAMILY_FORCE_TERMS):
        logger.info(f"Camada -1 (Family Priority) detectada: '{descricao}'. Categorizando como Família e Dependentes.")
        return "Família e Dependentes", "Apoio Familiar"

    # Camada -1.1: Proteção Hardcoded para Saúde (Evita conflito com Financeiro/Seguros)
    if any(_normalize(term) in desc_norm for term in _HEALTH_FORCE_TERMS):
        logger.info(f"Camada -1.1 (Saúde Priority) detectada: '{descricao}'. Categorizando como Saúde.")
        return "Saúde", "Plano de Saúde"

    # Camada -1.2: Proteção Hardcoded para Seguro Automóvel (Vai para Transporte)
    if any(_normalize(term) in desc_norm for term in _AUTO_FORCE_TERMS):
        logger.info(f"Camada -1.2 (Auto Priority) detectada: '{descricao}'. Categorizando como Transporte.")
        return "Transporte", "Seguro Automóvel"

    # Camada -1.3: Proteção Hardcoded para Vestuário e Beleza
    if any(_normalize(term) in desc_norm for term in _CLOTHING_FORCE_TERMS):
        logger.info(f"Camada -1.3 (Clothing Priority) detectada: '{descricao}'. Categorizando como Vestuário e Beleza.")
        sub = "Beleza e Cabelo" if any(x in desc_norm for x in ["manicure", "salao", "barbearia", "estetica", "beleza"]) else "Roupas"
        return "Vestuário e Beleza", sub

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
        
        # Ordenamos as subcategorias para priorizar Saúde e Transporte sobre Financeiro.
        # Prioridade máxima para Família para evitar que keywords genéricas sejam capturadas por outras categorias.
        def get_priority(cat_name):
            cat_norm = _normalize(cat_name)
            if "familia" in cat_norm or "dependente" in cat_norm or "apoio" in cat_norm: return -1 
            if cat_norm == "saude": return 0
            if cat_norm == "transporte": return 1
            return 5

        sorted_subs = sorted(res.data, key=lambda x: get_priority(x.get('category_name', '')))
        
        for sub in sorted_subs:
            keywords = sub.get("keywords", [])
            if any(_normalize(kw) in desc_norm for kw in keywords) or _normalize(sub["name"]) in desc_norm:
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