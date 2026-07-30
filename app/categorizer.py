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
    "padaria", "cafe", "café", "lanche", "bakery", "bistro", "gastronomia", "doceria",
    # Marketplaces genéricos — vendem de tudo, não dá para inferir categoria pela loja
    "mercadolivre", "mercado livre", "amazon", "shopee", "aliexpress",
    "magazine luiza", "magalu", "americanas.com", "submarino", "shein",
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

def _montar_taxonomia_prompt(categoria_restrita: str | None = None) -> str:
    """
    Monta a lista de categorias/subcategorias permitidas DINAMICAMENTE a partir de
    finbot_subcategories. Se categoria_restrita for informada, lista só as
    subcategorias daquela categoria (usado quando o merchant já tem categoria
    conhecida via aprendizado — reduz o universo de opções e melhora a precisão).
    """
    try:
        query = db.get_db().table("finbot_subcategories").select("name, finbot_categories(name)")
        res = query.order("name").execute()
        por_categoria: dict[str, list[str]] = {}
        for row in (res.data or []):
            cat = row["finbot_categories"]["name"]
            if categoria_restrita and cat != categoria_restrita:
                continue
            por_categoria.setdefault(cat, []).append(row["name"])
        linhas = [f"- {cat}: {', '.join(subs)}" for cat, subs in sorted(por_categoria.items())]
        return "\n".join(linhas)
    except Exception as e:
        logger.error(f"Erro ao montar taxonomia dinâmica para o prompt: {e}")
        return "- Outros: Outros"  # fallback mínimo, não deve travar a categorização


_taxonomia_cache: dict[str | None, str] = {}

def _get_taxonomia_prompt_cached(categoria_restrita: str | None = None) -> str:
    if categoria_restrita not in _taxonomia_cache:
        _taxonomia_cache[categoria_restrita] = _montar_taxonomia_prompt(categoria_restrita)
    return _taxonomia_cache[categoria_restrita]


def _invalidar_cache_taxonomia() -> None:
    global _taxonomia_cache
    _taxonomia_cache = {}


def _system_categorizer(categoria_restrita: str | None = None, permitir_perguntar: bool = True) -> str:
    if categoria_restrita:
        instrucao_categoria = f'A categoria já é conhecida: "{categoria_restrita}". Retorne SEMPRE essa categoria — sua única tarefa aqui é escolher a subcategoria mais adequada dentro dela.'
    else:
        instrucao_categoria = "Escolha a categoria E a subcategoria mais adequadas."

    instrucao_ambiguidade = (
        "\nIMPORTANTE: Se a descrição contiver termos como 'Restaurante', 'Bar', 'Café', 'Padaria', 'Pub' ou 'Lanche', "
        'e a categoria NÃO for conhecida previamente, você DEVE retornar obrigatoriamente:\n'
        '{"categoria": "Perguntar", "subcategoria": "Perguntar"}\n'
        if permitir_perguntar else
        "\nMesmo que a descrição pareça ambígua (ex: 'Restaurante', 'Bar', 'Café'), escolha a subcategoria MAIS PROVÁVEL "
        "dentro da lista oficial — não há usuário disponível para confirmar agora, então uma estimativa razoável é "
        "melhor do que não categorizar.\n"
    )

    return f"""
Você é o motor de classificação interna do FinBot. Sua única tarefa é ler a descrição de um gasto e mapeá-lo para uma SUBCATEGORIA e CATEGORIA válidas do sistema.

{instrucao_categoria}

SUBCATEGORIAS E CATEGORIAS PERMITIDAS NO SISTEMA (lista oficial, vinda do banco de dados — não invente nomes fora desta lista):
{_get_taxonomia_prompt_cached(categoria_restrita)}

ATENÇÃO: NUNCA use a categoria 'Pessoal' ou 'Outros' se houver uma opção melhor na lista acima.
{instrucao_ambiguidade}
Responda EXCLUSIVAMENTE com um JSON no formato estrito, usando exatamente os nomes da lista acima:
{{"categoria": "Nome da Categoria", "subcategoria": "Nome da Subcategoria"}}
"""

async def categorizar_gasto_hibrido(
    user_phone: str, descricao: str, fallback: tuple[str, str] | None = None,
    permitir_melhor_esforco: bool = False,
) -> tuple[str, str]:
    """
    Fluxo de camadas para categorização.
    permitir_melhor_esforco: quando True (ex: sincronização Pluggy, onde não há
    como perguntar ao usuário em tempo real), termos ambíguos (Camada 0) não
    travam mais em "Perguntar" — o fluxo continua tentando Camadas 1-3 para achar
    a melhor subcategoria possível (ex: "Barri Cafeteria" bate na keyword de
    "Café" mesmo sendo um termo listado como ambíguo). Quando False (fluxo de
    chat/WhatsApp, onde o usuário está disponível), mantém o comportamento
    original de interromper e pedir confirmação humana.
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
        return "Transporte", "Seguro Auto"

    # Camada -1.3: Proteção Hardcoded para Vestuário e Beleza
    if any(_normalize(term) in desc_norm for term in _CLOTHING_FORCE_TERMS):
        logger.info(f"Camada -1.3 (Clothing Priority) detectada: '{descricao}'. Categorizando como Vestuário e Beleza.")
        if "manicure" in desc_norm or "pedicure" in desc_norm or "unha" in desc_norm:
            sub = "Manicure"
        elif any(x in desc_norm for x in ["salao", "barbearia", "barba", "cabelo", "estetica", "beleza"]):
            sub = "Cabeleireiro"
        else:
            sub = "Roupa"
        return "Vestuário e Beleza", sub

    # Camada 0: Bloqueio de Ambiguidade — só interrompe se houver um usuário
    # disponível para responder. Em modo melhor-esforço, ignora e segue tentando
    # resolver pelas camadas seguintes (o keyword-match da Camada 2 já cobre boa
    # parte desses casos, ex: "Cafeteria" bate na keyword "cafe" de Café).
    # Remove padrões de "código de barras" (comuns em boletos) antes de checar
    # ambiguidade, para "cod bar"/"codigo de barras" não disparar falso positivo
    # no termo "bar" (ex: "VIVO-SP TELESP CELULAR-COD BAR" não é um bar).
    desc_norm_sem_ruido = re.sub(r"\bcod(igo)?\s*bar(ra)?[a-z]*\b", "", desc_norm)
    ambiguo = any(_normalize(term) in desc_norm_sem_ruido for term in _AMBIGUOUS_TERMS)
    if ambiguo and not permitir_melhor_esforco:
        logger.info(f"Camada 0 (Ambiguidade) detectada: '{descricao}'. Forçando interrupção para pergunta.")
        return "Perguntar", "Perguntar"
    elif ambiguo:
        logger.info(f"Camada 0 (Ambiguidade) detectada mas ignorada (modo melhor-esforço): '{descricao}'.")
    
    # Camada 1: Tem CATEGORIA já aprendida para esse Merchant?
    # Se além da categoria já existe uma subcategoria-dica de uma resolução
    # anterior EXATA desse mesmo merchant, usa ela direto — evita chamar
    # keyword/LLM de novo para o mesmo estabelecimento que já foi resolvido.
    # Só recorre a Camada 2/3 na primeira vez que esse merchant aparece.
    mapping = db.get_user_merchant_mapping(user_phone, desc_norm)
    categoria_conhecida = mapping["category_name"] if mapping else None
    if categoria_conhecida:
        logger.info(f"Camada 1 (User Merchant) — categoria conhecida: {desc_norm} -> {categoria_conhecida}")
        if mapping.get("subcategory_name_hint"):
            logger.info(f"Camada 1.5 (Subcategory Hint reutilizada): {desc_norm} -> {categoria_conhecida}/{mapping['subcategory_name_hint']}")
            return categoria_conhecida, mapping["subcategory_name_hint"]

    # Camada 2: Alguma keyword de subcategoria mapeia com o texto?
    # Se a categoria já é conhecida (Camada 1), busca só dentro dela — mais rápido
    # e mais preciso. Se não, busca em todas.
    try:
        query = db.get_db().table("finbot_subcategories").select("category_name, name, keywords")
        if categoria_conhecida:
            query = query.eq("category_name", categoria_conhecida)
        res = query.execute()

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
                logger.info(f"Camada 2 (Subcategory Keyword) resolvida: {desc_norm} -> {sub['category_name']}/{sub['name']}")
                db.save_user_merchant_mapping(user_phone, desc_norm, sub["category_name"], sub["name"])
                return sub["category_name"], sub["name"]
    except Exception as e:
        logger.error(f"Erro na Camada 2 de categorização: {e}")

    # Camada 2.5: Sugestão da extração inicial (PDF/Gemini) — só é confiável se a
    # subcategoria sugerida REALMENTE existir na taxonomia oficial (o extrator de
    # PDF usa texto livre para subcategoria, então pode sugerir algo inventado).
    if fallback and fallback[0] != "Outros":
        fb_categoria, fb_subcategoria = fallback
        if (not categoria_conhecida or fb_categoria == categoria_conhecida) and db.get_subcategory_id_by_name(fb_subcategoria):
            logger.info(f"Camada 2.5 (PDF Fallback, validado) utilizada: {descricao} -> {fb_categoria}/{fb_subcategoria}")
            return fb_categoria, fb_subcategoria
        else:
            logger.info(f"Camada 2.5 ignorada — fallback '{fb_categoria}/{fb_subcategoria}' não bate com a taxonomia oficial ou diverge da categoria já conhecida.")

    # Camada 3: LLM decide a subcategoria — restrita à categoria já conhecida, se houver
    logger.info(f"Camada 3 (LLM) acionada para local inédito: {descricao} (categoria conhecida: {categoria_conhecida})")
    try:
        response = await call_llm(
            system=_system_categorizer(categoria_restrita=categoria_conhecida, permitir_perguntar=not permitir_melhor_esforco),
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
        cat = categoria_conhecida or data.get("categoria", "Outros")
        sub = data.get("subcategoria", "Outros")

        if cat not in ["Outros", "Perguntar"] and sub not in ["Outros", "Perguntar"]:
            db.save_user_merchant_mapping(user_phone, descricao, cat, sub)

        return cat, sub
    except Exception as e:
        logger.error(f"Erro na Camada 3 (LLM): {e}")
        return (categoria_conhecida, "Outros") if categoria_conhecida else ("Outros", "Outros")