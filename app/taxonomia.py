# app/taxonomia.py
# Taxonomia v2 (fonte única no código). A fonte da verdade dos dados é o banco (finbot_categories / finbot_subcategories);
# aqui ficam só a lista de nomes válidos, atalhos de palavras para categoria e a tradução dos nomes antigos.
import unicodedata

A_CLASSIFICAR = ("Outros", "A classificar")
COMPRAS_ONLINE = ("Outros", "Compras Online")

# (categoria, emoji, movimento)
CATEGORIAS_V2 = [
    ('Moradia', '🏠', 'despesa'),
    ('Alimentação', '🍔', 'despesa'),
    ('Transporte', '🚗', 'despesa'),
    ('Viagens', '✈️', 'despesa'),
    ('Saúde', '💊', 'despesa'),
    ('Vestuário e Beleza', '👕', 'despesa'),
    ('Lazer e Cultura', '🎬', 'despesa'),
    ('Educação', '📚', 'despesa'),
    ('Família e Dependentes', '👨\u200d👩\u200d👧', 'despesa'),
    ('Presentes e Doações', '🎁', 'despesa'),
    ('Seguros', '🛡️', 'despesa'),
    ('Impostos', '🏛️', 'despesa'),
    ('Dívidas e Financiamentos', '🏦', 'despesa'),
    ('Tarifas e Juros', '💳', 'despesa'),
    ('Trabalho e Negócio', '💼', 'despesa'),
    ('Outros', '📦', 'despesa'),
    ('Receitas', '💰', 'receita'),
    ('Transferências', '🔁', 'transferencia'),
    ('Investimentos', '📈', 'investimento'),
]

CATEGORIAS = [c for c, _, _ in CATEGORIAS_V2]
# Categorias de despesa (as que o usuário escolhe ao registrar um gasto)
CATEGORIAS_DESPESA = [c for c, _, m in CATEGORIAS_V2 if m == 'despesa']

SUBCATEGORIAS = {
    'Moradia': ['Aluguel', 'Condomínio', 'Energia, Água e Gás', 'Internet e TV', 'Celular', 'Reforma e Manutenção', 'Móveis e Utensílios', 'Faxina e Serviços Domésticos'],
    'Alimentação': ['Mercado', 'Feira e Hortifruti', 'Padaria e Café', 'Restaurante', 'Delivery', 'Bar'],
    'Transporte': ['Aplicativo e Táxi', 'Combustível', 'Transporte Público', 'Estacionamento e Pedágio', 'Manutenção do Veículo'],
    'Viagens': ['Passagens', 'Hospedagem', 'Câmbio e Remessas', 'Outros em Viagem'],
    'Saúde': ['Plano de Saúde', 'Farmácia e Suplementos', 'Consultas e Exames', 'Terapia', 'Academia e Esportes'],
    'Vestuário e Beleza': ['Roupa', 'Calçado', 'Cabeleireiro e Barbearia', 'Manicure e Estética', 'Cosméticos'],
    'Lazer e Cultura': ['Streaming e Assinaturas', 'Cinema, Shows e Eventos', 'Jogos e Hobbies', 'Parques e Passeios'],
    'Educação': ['Escola e Faculdade', 'Cursos Online', 'Livros e Material', 'Idiomas'],
    'Família e Dependentes': ['Mesada', 'Pensão', 'Apoio Familiar'],
    'Presentes e Doações': ['Presentes', 'Doações', 'Dízimo e Ofertas'],
    'Seguros': ['Seguro Auto', 'Seguro de Vida', 'Seguro Residencial', 'Outros Seguros'],
    'Impostos': ['IPVA', 'IPTU', 'Imposto de Renda', 'INSS e Contribuições', 'DAS / Simples Nacional', 'Outros Impostos'],
    'Dívidas e Financiamentos': ['Empréstimos', 'Financiamento de Veículo', 'Financiamento Imobiliário', 'Parcelamento de Fatura'],
    'Tarifas e Juros': ['Tarifas Bancárias', 'Anuidade de Cartão', 'IOF e Câmbio', 'Juros e Multas'],
    'Trabalho e Negócio': ['Ferramentas e Software', 'Contabilidade e Serviços', 'Folha e Pró-labore', 'Fornecedores', 'Marketing e Escritório'],
    'Outros': ['Compras Online', 'A classificar'],
    'Receitas': ['Salário e Pró-labore', 'Freelance e Bônus', 'Aposentadoria e Benefícios', 'Rendimentos', 'Reembolso e Estorno', 'Transferências de Terceiros', 'Outras Receitas'],
    'Transferências': ['Entre Contas Próprias', 'Pagamento de Fatura', 'Empréstimo a Terceiros', 'Verificação (≤ R$ 1)', 'Estorno Compensado'],
    'Investimentos': ['Aporte em Investimento', 'Resgate de Investimento'],
}

VALIDAS = {(c, s) for c, subs in SUBCATEGORIAS.items() for s in subs}


def lista_para_prompt() -> str:
    """Categorias de despesa com suas subcategorias, uma por linha, para a IA do PDF escolher só nomes oficiais
    (assim a classificação já vem na leitura do extrato e poupa chamadas de IA e de busca na web depois)."""
    linhas = []
    for c in CATEGORIAS_DESPESA:
        subs = [x for x in SUBCATEGORIAS.get(c, []) if (c, x) != A_CLASSIFICAR]
        if subs:
            linhas.append(f"   - {c}: {', '.join(subs)}")
    return "\n".join(linhas)

# Nomes antigos (categoria, subcategoria) -> nomes novos. Usado em tudo que ainda chega com a taxonomia velha (PDF, IA, atalhos).
LEGADO = {
    ('Alimentação', 'Cafe'): ('Alimentação', 'Padaria e Café'),
    ('Alimentação', 'Café'): ('Alimentação', 'Padaria e Café'),
    ('Alimentação', 'Delivery'): ('Alimentação', 'Delivery'),
    ('Alimentação', 'Feira'): ('Alimentação', 'Feira e Hortifruti'),
    ('Alimentação', 'Feira e Hortifruti'): ('Alimentação', 'Feira e Hortifruti'),
    ('Alimentação', 'Mercado'): ('Alimentação', 'Mercado'),
    ('Alimentação', 'Outros'): ('Alimentação', 'Restaurante'),
    ('Alimentação', 'Padaria'): ('Alimentação', 'Padaria e Café'),
    ('Alimentação', 'Restaurante'): ('Alimentação', 'Restaurante'),
    ('Educação', 'Curso Online'): ('Educação', 'Cursos Online'),
    ('Educação', 'Material'): ('Educação', 'Livros e Material'),
    ('Empresa', 'Contabilidade'): ('Trabalho e Negócio', 'Contabilidade e Serviços'),
    ('Empresa', 'Ferramentas e Software'): ('Trabalho e Negócio', 'Ferramentas e Software'),
    ('Empresa', 'Folha e Pró-labore'): ('Receitas', 'Transferências de Terceiros'),
    ('Empresa', 'Fornecedores'): ('Trabalho e Negócio', 'Fornecedores'),
    ('Empresa', 'Impostos PJ'): ('Impostos', 'DAS / Simples Nacional'),
    ('Empresa', 'Receita'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Empresa', 'Streaming'): ('Trabalho e Negócio', 'Ferramentas e Software'),
    ('Extra', 'Outros'): ('Receitas', 'Outras Receitas'),
    ('Família e Dependentes', 'Apoio Familiar'): ('Família e Dependentes', 'Apoio Familiar'),
    ('Família e Dependentes', 'Empréstimo Pessoal'): ('Família e Dependentes', 'Apoio Familiar'),
    ('Financeiro', 'Empresa'): ('Outros', 'A classificar'),
    ('Financeiro', 'Empréstimo e Financiamento'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Financeiro', 'Estorno'): ('Receitas', 'Reembolso e Estorno'),
    ('Financeiro', 'Impostos PJ'): ('Impostos', 'DAS / Simples Nacional'),
    ('Financeiro', 'Pagamento'): ('Outros', 'A classificar'),
    ('Financeiro', 'Rendimento'): ('Receitas', 'Rendimentos'),
    ('Financeiro', 'Salario'): ('Receitas', 'Salário e Pró-labore'),
    ('Financeiro', 'Seguro'): ('Seguros', 'Outros Seguros'),
    ('Financeiro', 'Tarifa'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Financeiro', 'Transferência'): ('Outros', 'A classificar'),
    ('Lazer', 'Bar'): ('Alimentação', 'Bar'),
    ('Lazer', 'Cafe'): ('Alimentação', 'Padaria e Café'),
    ('Lazer', 'Café'): ('Alimentação', 'Padaria e Café'),
    ('Lazer', 'Cinema e Shows'): ('Lazer e Cultura', 'Cinema, Shows e Eventos'),
    ('Lazer', 'Empréstimo Pessoal'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Lazer', 'Entretenimento'): ('Lazer e Cultura', 'Jogos e Hobbies'),
    ('Lazer', 'Festas/Eventos'): ('Lazer e Cultura', 'Cinema, Shows e Eventos'),
    ('Lazer', 'Geral'): ('Outros', 'A classificar'),
    ('Lazer', 'Hobbies e Jogos'): ('Lazer e Cultura', 'Jogos e Hobbies'),
    ('Lazer', 'Padaria'): ('Alimentação', 'Padaria e Café'),
    ('Lazer', 'Parque'): ('Lazer e Cultura', 'Parques e Passeios'),
    ('Lazer', 'Restaurante'): ('Alimentação', 'Restaurante'),
    ('Lazer', 'Restaurante lazer'): ('Alimentação', 'Restaurante'),
    ('Lazer', 'Roupa'): ('Vestuário e Beleza', 'Roupa'),
    ('Lazer', 'Streaming'): ('Lazer e Cultura', 'Streaming e Assinaturas'),
    ('Lazer', 'Viagem'): ('Viagens', 'Outros em Viagem'),
    ('Moradia', 'Casa'): ('Moradia', 'Reforma e Manutenção'),
    ('Moradia', 'Celular'): ('Moradia', 'Celular'),
    ('Moradia', 'Condomínio'): ('Moradia', 'Condomínio'),
    ('Moradia', 'Contas'): ('Moradia', 'Celular'),
    ('Moradia', 'Faxina'): ('Moradia', 'Faxina e Serviços Domésticos'),
    ('Moradia', 'Reforma e Manutenção'): ('Moradia', 'Reforma e Manutenção'),
    ('Moradia', 'Utensílios'): ('Moradia', 'Móveis e Utensílios'),
    ('Moradia', 'Utensílos'): ('Moradia', 'Móveis e Utensílios'),
    ('Outros', 'Compras Online'): ('Outros', 'Compras Online'),
    ('Outros', 'Comércio Geral'): ('Outros', 'Compras Online'),
    ('Outros', 'Geral'): ('Outros', 'Compras Online'),
    ('Outros', 'Outros'): ('Outros', 'A classificar'),
    ('Outros', 'Pagamento'): ('Outros', 'A classificar'),
    ('Outros', 'Restaurante'): ('Alimentação', 'Restaurante'),
    ('Pets', 'Banho e Tosa'): ('Vestuário e Beleza', 'Manicure e Estética'),
    ('Pets', 'Plano Pet'): ('Saúde', 'Plano de Saúde'),
    ('Pets', 'Ração'): ('Alimentação', 'Mercado'),
    ('Pets', 'Veterinário'): ('Saúde', 'Consultas e Exames'),
    ('Presentes e Doações', 'Presentes'): ('Presentes e Doações', 'Presentes'),
    ('Receita', 'Contas'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Empréstimo Pessoal'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Empréstimo e Financiamento'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Estacionamento'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Folha e Pró-labore'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Geral'): ('Receitas', 'Outras Receitas'),
    ('Receita', 'Outros'): ('Receitas', 'Outras Receitas'),
    ('Receita', 'Ração'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Receita'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Restaurante'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receita', 'Tarifa'): ('Receitas', 'Rendimentos'),
    ('Receita', 'Viagem'): ('Transferências', 'Verificação (≤ R$ 1)'),
    ('Receitas', 'Freelance e Bônus'): ('Receitas', 'Freelance e Bônus'),
    ('Receitas', 'Outros'): ('Receitas', 'Outras Receitas'),
    ('Receitas', 'Reembolso'): ('Receitas', 'Reembolso e Estorno'),
    ('Receitas', 'Rendimento'): ('Receitas', 'Rendimentos'),
    ('Receitas', 'Salário'): ('Receitas', 'Salário e Pró-labore'),
    ('Receitas', 'Transferência Recebida'): ('Receitas', 'Transferências de Terceiros'),
    ('Salario', 'Salario'): ('Receitas', 'Salário e Pró-labore'),
    ('Salário', 'Outros'): ('Receitas', 'Salário e Pró-labore'),
    ('Saúde', 'Academia'): ('Saúde', 'Academia e Esportes'),
    ('Saúde', 'Farmácia'): ('Saúde', 'Farmácia e Suplementos'),
    ('Saúde', 'Médico'): ('Saúde', 'Consultas e Exames'),
    ('Saúde', 'Pet'): ('Saúde', 'Consultas e Exames'),
    ('Saúde', 'Plano de Saúde'): ('Saúde', 'Plano de Saúde'),
    ('Saúde', 'Suplemento'): ('Saúde', 'Farmácia e Suplementos'),
    ('Transporte', 'Aplicativo'): ('Transporte', 'Aplicativo e Táxi'),
    ('Transporte', 'Combustível'): ('Transporte', 'Combustível'),
    ('Transporte', 'Estacionamento'): ('Transporte', 'Estacionamento e Pedágio'),
    ('Transporte', 'IPVA'): ('Impostos', 'IPVA'),
    ('Transporte', 'Oficina'): ('Transporte', 'Manutenção do Veículo'),
    ('Transporte', 'Seguro Auto'): ('Seguros', 'Seguro Auto'),
    ('Transporte', 'Transporte Público'): ('Transporte', 'Transporte Público'),
    ('Transporte', 'Viagem'): ('Viagens', 'Passagens'),
    ('Vestuario e Beleza', 'Cabeleireiro'): ('Vestuário e Beleza', 'Cabeleireiro e Barbearia'),
    ('Vestuario e Beleza', 'Manicure'): ('Vestuário e Beleza', 'Manicure e Estética'),
    ('Vestuario e Beleza', 'Presente'): ('Presentes e Doações', 'Presentes'),
    ('Vestuario e Beleza', 'Roupa'): ('Vestuário e Beleza', 'Roupa'),
    ('Vestuário e Beleza', 'Cabeleireiro'): ('Vestuário e Beleza', 'Cabeleireiro e Barbearia'),
    ('Vestuário e Beleza', 'Cabelereiro'): ('Vestuário e Beleza', 'Cabeleireiro e Barbearia'),
    ('Vestuário e Beleza', 'Cosmetico'): ('Vestuário e Beleza', 'Cosméticos'),
    ('Vestuário e Beleza', 'Cosméticos'): ('Vestuário e Beleza', 'Cosméticos'),
    ('Vestuário e Beleza', 'Custuteira'): ('Vestuário e Beleza', 'Roupa'),
    ('Vestuário e Beleza', 'Empresa'): ('Outros', 'A classificar'),
    ('Vestuário e Beleza', 'Manicure'): ('Vestuário e Beleza', 'Manicure e Estética'),
    ('Vestuário e Beleza', 'Padaria'): ('Alimentação', 'Padaria e Café'),
    ('Vestuário e Beleza', 'Roupa'): ('Vestuário e Beleza', 'Roupa'),
    ('Vestuário e Beleza', 'Veterinário'): ('Saúde', 'Consultas e Exames'),
}

# Atalhos: como o usuário costuma CHAMAR cada categoria numa frase ("gastos lazer", "limite família").
ATALHOS = {
    'lazer': 'Lazer e Cultura', 'cultura': 'Lazer e Cultura', 'vestuario': 'Vestuário e Beleza', 'beleza': 'Vestuário e Beleza', 'roupa': 'Vestuário e Beleza', 'roupas': 'Vestuário e Beleza',
    'familia': 'Família e Dependentes', 'dependentes': 'Família e Dependentes', 'presentes': 'Presentes e Doações', 'doacoes': 'Presentes e Doações',
    'dividas': 'Dívidas e Financiamentos', 'divida': 'Dívidas e Financiamentos', 'financiamentos': 'Dívidas e Financiamentos', 'tarifas': 'Tarifas e Juros', 'juros': 'Tarifas e Juros',
    'trabalho': 'Trabalho e Negócio', 'negocio': 'Trabalho e Negócio', 'empresa': 'Trabalho e Negócio', 'viagem': 'Viagens', 'viagens': 'Viagens', 'imposto': 'Impostos', 'impostos': 'Impostos',
    'seguro': 'Seguros', 'seguros': 'Seguros', 'receita': 'Receitas', 'investimento': 'Investimentos', 'investimentos': 'Investimentos', 'transferencia': 'Transferências', 'transferencias': 'Transferências',
}

def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())

def traduzir(categoria: str | None, subcategoria: str | None) -> tuple[str, str]:
    """Qualquer par (novo ou antigo) -> par válido na taxonomia v2. Sem tradução, "Outros > A classificar"."""
    par = (categoria or "", subcategoria or "")
    if par in VALIDAS:
        return par
    if par in LEGADO:
        return LEGADO[par]
    # nome certo de categoria com subcategoria desconhecida: tenta achar a subcategoria só pelo nome
    for cat, subs in SUBCATEGORIAS.items():
        if _norm(par[1]) in {_norm(s) for s in subs}:
            return cat, next(s for s in subs if _norm(s) == _norm(par[1]))
    if par[0] == "Receitas":   # entrada com subcategoria desconhecida continua sendo receita
        return "Receitas", "Outras Receitas"
    return A_CLASSIFICAR

def categoria_valida(categoria: str | None) -> str | None:
    """Nome (novo ou como o usuário costuma chamar) -> nome oficial da categoria, ou None."""
    n = _norm(categoria or "")
    for c in CATEGORIAS:
        if _norm(c) == n:
            return c
    return ATALHOS.get(n)

def detectar_categoria(texto_norm: str) -> str | None:
    """Procura numa frase (já normalizada) o nome de uma categoria ou um atalho. Nome completo ganha de atalho."""
    for c in CATEGORIAS:
        if _norm(c) in texto_norm:
            return c
    palavras = set(texto_norm.split())
    for atalho, cat in ATALHOS.items():
        if atalho in palavras:
            return cat
    return None
