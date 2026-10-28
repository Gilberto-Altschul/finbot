# app/categorizer.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Categorizer — Motor Híbrido de Subcategorias e Inteligência de Merchants
# ─────────────────────────────────────────────────────────────────────────────
import contextvars
import json
import logging
import app.database as db
import re
from app.llm import call_llm
from app.utils import _normalize, SISTEMA_CATEGORIAS
from app.merchant import contem_termo, familia_ambigua
from app.taxonomia import A_CLASSIFICAR, COMPRAS_ONLINE, traduzir

logger = logging.getLogger(__name__)

# De onde veio o último palpite: "llm" = a IA chutou um estabelecimento inédito (sem keyword nem histórico).
# Quem chama (classificador) usa isso para PERGUNTAR em vez de gravar sozinho.
ORIGEM_PALPITE: contextvars.ContextVar = contextvars.ContextVar("origem_palpite", default=None)
CERTEZA_IA: contextvars.ContextVar = contextvars.ContextVar("certeza_ia", default=None)   # 0..1, quando a IA decidiu sozinha
LIMIAR_IA_AUTO = 0.5     # a IA classifica sem pergunta quando a certeza declarada é MAIOR que isso


def _certeza(valor) -> float | None:
    """Certeza declarada pela IA como fração 0..1 (aceita 85, '85%', 0.85). Ilegível -> None (= dúvida)."""
    try:
        x = float(str(valor).strip().rstrip("%").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if x > 1:
        x /= 100
    return x if 0 <= x <= 1 else None


# Termos curtos que viram falso positivo quando buscados como pedaço de outra palavra:
# 'cea' em "mercearia", 'amil' em "família", 'bar' em "Barbosa"/"Barra"/"Barueri", 'pub' em "publicidade".
# Só estes exigem palavra inteira; os demais (ex.: 'cafe' em "cafeteria") continuam por substring.
_PALAVRA_INTEIRA = {"bar", "pub", "cea", "amil"}


def _tem_termo(texto_norm: str, termos: list[str]) -> bool:
    for t in termos:
        tn = _normalize(t)
        if contem_termo(texto_norm, tn, palavra_inteira=tn in _PALAVRA_INTEIRA):
            return True
    return False


def _keyword_casa(kw: str, desc_norm: str) -> bool:
    """Keyword de subcategoria contra a descrição.
    - "das" é o imposto do MEI, mas também é a preposição ("Sabor Das Massas"): só vale com contexto.
    - Keyword curta (até 3 letras) só casa como palavra inteira ("gas" não pode casar dentro de "magazine").
    - Keyword longa segue por substring, para "cafe" casar com "cafeteria"."""
    if not kw:
        return False
    if kw == "das":
        return re.search(r"(?<![a-z])(pgdas|das[\s-]+(mei|simples)|guia[\s-]+(do[\s-]+)?das)(?![a-z])", desc_norm) is not None
    if len(kw) < 4:
        borda = "a-z0-9" if any(c.isdigit() for c in kw) else "a-z"
        return re.search(rf"(?<![{borda}]){re.escape(kw)}(?![{borda}])", desc_norm) is not None
    return kw in desc_norm


# Marketplaces: vendem de tudo, não dá para inferir categoria pela loja. Taxonomia v2: vão para
# Outros > Compras Online (sem pergunta). Restaurante/bar/café/padaria são sempre Alimentação.
_AMBIGUOUS_TERMS = [
    "mercadolivre", "mercado livre", "amazon", "shopee", "aliexpress",
    "magazine luiza", "magalu", "americanas.com", "submarino", "shein",
]

# Termos que forçam a categoria Saúde para evitar conflitos com 'Seguro' no Financeiro
_HEALTH_FORCE_TERMS = [
    "plano de saude", "convenio", "unimed", "bradesco saude", "sulamerica", "amil", "odontoprev", "hapvida"
]

# Termos que forçam Seguros > Seguro Auto
_AUTO_FORCE_TERMS = [
    "seguro auto", "seguro automovel", "seguro veiculo", "porto seguro", "azul seguros", "tokio marine", "allianz"
]

# Termos que forçam a categoria Vestuário e Beleza para evitar 'Pessoal'
_CLOTHING_FORCE_TERMS = [
    "roupa", "vestuario", "calcado", "tenis", "sapato", "zara", "renner", "cea", "riachuelo", "loja de roupa", "lingerie",
    "manicure", "pedicure", "salao", "cabelo", "estetica", "barbearia", "beleza", "cosmetico", "zerezes"
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
            if cat in ("Receitas", "Transferências", "Investimentos"):   # o movimento decide; o categorizador só trata gastos
                continue
            if categoria_restrita and cat != categoria_restrita:
                continue
            por_categoria.setdefault(cat, []).append(row["name"])
        linhas = [f"- {cat}: {', '.join(subs)}" for cat, subs in sorted(por_categoria.items())]
        return "\n".join(linhas)
    except Exception as e:
        logger.error(f"Erro ao montar taxonomia dinâmica para o prompt: {e}")
        return "- Outros: Compras Online, A classificar"  # fallback mínimo, não deve travar a categorização


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

    # `permitir_perguntar` fica só por compatibilidade: na taxonomia v2 a IA nunca "pergunta"; na dúvida real usa
    # Outros > A classificar e o bot pergunta ao usuário depois.
    instrucao_ambiguidade = (
        "\nREGRAS DE DESEMPATE (cada gasto tem UMA categoria correta; vale a intenção, não a forma de pagamento):\n"
        "- Comer fora (restaurante, bar, café, padaria, delivery) é SEMPRE Alimentação.\n"
        "- Plano de saúde (de pessoa ou de pet) é Saúde; seguro de carro, vida, casa ou celular é Seguros.\n"
        "- Imposto ou tributo (IPTU, IPVA, IR, DAS, INSS) é Impostos, mesmo se for da casa ou do carro.\n"
        "- Hotel, passagem, câmbio e despesa de viagem são Viagens.\n"
        "- Parcela de empréstimo ou financiamento é Dívidas e Financiamentos; juros, IOF, multa e tarifa são Tarifas e Juros.\n"
        "- Marketplace (Mercado Livre, Amazon, Shopee) é Outros > Compras Online.\n"
        "- Se realmente não der para saber, responda {\"categoria\": \"Outros\", \"subcategoria\": \"A classificar\"}; "
        "é melhor do que chutar.\n"
    )

    return f"""
Você é o motor de classificação interna do FinBot. Sua única tarefa é ler a descrição de um gasto e mapeá-lo para uma SUBCATEGORIA e CATEGORIA válidas do sistema.

{instrucao_categoria}

SUBCATEGORIAS E CATEGORIAS PERMITIDAS NO SISTEMA (lista oficial, vinda do banco de dados — não invente nomes fora desta lista):
{_get_taxonomia_prompt_cached(categoria_restrita)}

ATENÇÃO: use 'Outros' só quando nenhuma outra categoria servir. Receitas, transferências e investimentos não fazem parte desta lista.
{instrucao_ambiguidade}
Responda EXCLUSIVAMENTE com um JSON no formato estrito, usando exatamente os nomes da lista acima:
{{"categoria": "Nome da Categoria", "subcategoria": "Nome da Subcategoria", "certeza": 85}}

Campo "certeza": número inteiro de 0 a 100 com a sua confiança de que a classificação está certa. Seja honesto: acima de 50 só
se você reconhece o estabelecimento ou o termo é claro (livraria, sebo, farmácia, posto, seguro); se estiver deduzindo pelo nome
ou chutando, use 50 ou menos.
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
    ORIGEM_PALPITE.set(None)
    CERTEZA_IA.set(None)

    # Camada -1: Proteção Hardcoded para Família e Dependentes (Prioridade Máxima)
    if _tem_termo(desc_norm, _FAMILY_FORCE_TERMS):
        logger.info(f"Camada -1 (Family Priority) detectada: '{descricao}'. Categorizando como Família e Dependentes.")
        return "Família e Dependentes", "Apoio Familiar"

    # Camada -1.1: Proteção Hardcoded para Saúde (Evita conflito com Financeiro/Seguros)
    if _tem_termo(desc_norm, _HEALTH_FORCE_TERMS):
        logger.info(f"Camada -1.1 (Saúde Priority) detectada: '{descricao}'. Categorizando como Saúde.")
        return "Saúde", "Plano de Saúde"

    # Camada -1.2: Proteção Hardcoded para Seguro Automóvel (Vai para Transporte)
    if _tem_termo(desc_norm, _AUTO_FORCE_TERMS):
        logger.info(f"Camada -1.2 (Auto Priority) detectada: '{descricao}'. Categorizando como Seguros.")
        return "Seguros", "Seguro Auto"

    # Camada -1.3: Proteção Hardcoded para Vestuário e Beleza
    if _tem_termo(desc_norm, _CLOTHING_FORCE_TERMS):
        logger.info(f"Camada -1.3 (Clothing Priority) detectada: '{descricao}'. Categorizando como Vestuário e Beleza.")
        if "manicure" in desc_norm or "pedicure" in desc_norm or "unha" in desc_norm or "estetica" in desc_norm:
            sub = "Manicure e Estética"
        elif any(x in desc_norm for x in ["salao", "barbearia", "barba", "cabelo", "beleza"]):
            sub = "Cabeleireiro e Barbearia"
        elif "calcado" in desc_norm or "tenis" in desc_norm or "sapato" in desc_norm:
            sub = "Calçado"
        elif "cosmetico" in desc_norm:
            sub = "Cosméticos"
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
    if _tem_termo(desc_norm_sem_ruido, _AMBIGUOUS_TERMS):
        logger.info(f"Camada 0 (Marketplace) detectada: '{descricao}'. Categorizando como Outros > Compras Online.")
        return COMPRAS_ONLINE
    
    # Camada 1: Tem CATEGORIA já aprendida para esse Merchant?
    # Se além da categoria já existe uma subcategoria-dica de uma resolução
    # anterior EXATA desse mesmo merchant, usa ela direto — evita chamar
    # keyword/LLM de novo para o mesmo estabelecimento que já foi resolvido.
    # Só recorre a Camada 2/3 na primeira vez que esse merchant aparece.
    eh_marketplace = familia_ambigua(descricao) is not None
    mapping = None if eh_marketplace else db.get_user_merchant_mapping(user_phone, desc_norm)
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
            if any(_keyword_casa(_normalize(kw), desc_norm) for kw in keywords) or _keyword_casa(_normalize(sub["name"]), desc_norm):
                logger.info(f"Camada 2 (Subcategory Keyword) resolvida: {desc_norm} -> {sub['category_name']}/{sub['name']}")
                if not eh_marketplace:
                    db.save_user_merchant_mapping(user_phone, desc_norm, sub["category_name"], sub["name"])
                return sub["category_name"], sub["name"]
    except Exception as e:
        logger.error(f"Erro na Camada 2 de categorização: {e}")

    # Camada 2.5: Sugestão da extração inicial (PDF/Gemini) — só é confiável se a
    # subcategoria sugerida REALMENTE existir na taxonomia oficial (o extrator de
    # PDF usa texto livre para subcategoria, então pode sugerir algo inventado).
    if fallback and traduzir(*fallback) != A_CLASSIFICAR:
        fb_categoria, fb_subcategoria = traduzir(*fallback)
        if (not categoria_conhecida or fb_categoria == categoria_conhecida) and db.get_subcategory_id_by_name(fb_subcategoria):
            logger.info(f"Camada 2.5 (PDF Fallback, validado) utilizada: {descricao} -> {fb_categoria}/{fb_subcategoria}")
            return fb_categoria, fb_subcategoria
        else:
            logger.info(f"Camada 2.5 ignorada — fallback '{fb_categoria}/{fb_subcategoria}' não bate com a taxonomia oficial ou diverge da categoria já conhecida.")

    # Camada 3: LLM decide a subcategoria — restrita à categoria já conhecida, se houver
    logger.info(f"Camada 3 (LLM) acionada para local inédito: {descricao} (categoria conhecida: {categoria_conhecida})")
    try:
        mensagem = f"Classifique a descrição: '{descricao}'"
        dica = None
        if not categoria_conhecida:
            # Lugar inédito: o que a web diz sobre ele ajuda a IA a acertar (ex.: "ZEREZES SHOPPING ELDOR" = ótica).
            try:
                from app import pesquisa_web
                dica = await pesquisa_web.descrever(descricao)
            except Exception as e:
                logger.warning(f"Pesquisa web falhou: {e}")
                dica = None
            if dica:
                mensagem += f"\nO que a busca na web diz sobre esse estabelecimento: {dica}"
        response = await call_llm(
            system=_system_categorizer(categoria_restrita=categoria_conhecida, permitir_perguntar=not permitir_melhor_esforco),
            history=[],
            message=mensagem,
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
        cat, sub = traduzir(categoria_conhecida or data.get("categoria"), data.get("subcategoria"))   # só nome da taxonomia v2
        if categoria_conhecida and cat != categoria_conhecida:
            cat, sub = A_CLASSIFICAR

        # Palpite da IA para um lugar inédito NÃO é gravado como "aprendido": só vale depois que o usuário
        # confirmar (senão um chute como "Salute" -> Saúde vira regra permanente sem ninguém ver).
        # Exceção: se a web identificou o estabelecimento (dica), a classificação não é chute no escuro — vale direto
        # e fica aprendida (o usuário corrige com "acertar" se discordar), sem pergunta.
        # Também vale direto quando a própria IA declara MAIS de 50% de certeza (LIMIAR_IA_AUTO); a certeza fica gravada
        # no lançamento (confidence). Com 50% ou menos, sem o campo ou ilegível, continua sendo chute: revisão.
        certeza = _certeza(data.get("certeza"))
        confiante = certeza is not None and certeza > LIMIAR_IA_AUTO
        if (cat, sub) != A_CLASSIFICAR and not categoria_conhecida and not dica and not confiante:
            ORIGEM_PALPITE.set("llm")
        elif (cat, sub) != A_CLASSIFICAR and not eh_marketplace:
            if confiante and not dica and not categoria_conhecida:
                CERTEZA_IA.set(round(certeza, 2))
            db.save_user_merchant_mapping(user_phone, descricao, cat, sub)

        return cat, sub
    except Exception as e:
        logger.error(f"Erro na Camada 3 (LLM): {e}")
        return A_CLASSIFICAR