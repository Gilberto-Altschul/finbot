"""MCC (código do tipo de estabelecimento que a bandeira informa, `creditCardMetadata.payeeMCC` na Pluggy) -> taxonomia.

Só entram códigos de significado claro. O que é genérico ou ambíguo (5331 lojas de variedade/Mercado Livre, 5999, 7399,
5817/5818 bens digitais, 5968 assinaturas, 8043 ótica...) fica de fora e segue para histórico, palavras-chave, busca e revisão.
O MCC vale DEPOIS do que o usuário já confirmou e das palavras-chave, e ANTES da IA."""
_TAB: dict[str, tuple[str, str]] = {}


def _add(codigos, cat: str, sub: str):
    for c in codigos:
        _TAB[str(c)] = (cat, sub)


_add([5411, 5422, 5441, 5451, 5499, 5300, 5310], "Alimentação", "Mercado")
_add([5462], "Alimentação", "Padaria e Café")
_add([5811, 5812, 5814], "Alimentação", "Restaurante")
_add([5813], "Alimentação", "Bar")
_add([5912], "Saúde", "Farmácia e Suplementos")
_add([8011, 8021, 8031, 8041, 8042, 8049, 8050, 8062, 8071, 8099], "Saúde", "Consultas e Exames")
_add([7997], "Saúde", "Academia e Esportes")
_add([5541, 5542, 5983], "Transporte", "Combustível")
_add([4121], "Transporte", "Aplicativo e Táxi")
_add([4111, 4112, 4131], "Transporte", "Transporte Público")
_add([7523, 4784], "Transporte", "Estacionamento e Pedágio")
_add([5533, 7531, 7534, 7535, 7538, 7542], "Transporte", "Manutenção do Veículo")
_add([4511, 4722] + list(range(3000, 3300)), "Viagens", "Passagens")
_add([7011] + list(range(3500, 4000)), "Viagens", "Hospedagem")
_add([4814], "Moradia", "Celular")
_add([4900], "Moradia", "Energia, Água e Gás")
_add([5712, 5719, 5722, 5200], "Moradia", "Móveis e Utensílios")
_add([4899], "Lazer e Cultura", "Streaming e Assinaturas")
_add([7832], "Lazer e Cultura", "Cinema, Shows e Eventos")
_add([7922, 7929, 7941], "Lazer e Cultura", "Cinema, Shows e Eventos")
_add([7991, 7996, 7998], "Lazer e Cultura", "Parques e Passeios")
_add([5816, 5945], "Lazer e Cultura", "Jogos e Hobbies")
_add([5651, 5691, 5699, 5611, 5621, 5631, 5641, 5137, 5139], "Vestuário e Beleza", "Roupa")
_add([5661], "Vestuário e Beleza", "Calçado")
_add([7230], "Vestuário e Beleza", "Cabeleireiro e Barbearia")
_add([7298], "Vestuário e Beleza", "Manicure e Estética")
_add([5977], "Vestuário e Beleza", "Cosméticos")
_add([8211, 8220], "Educação", "Escola e Faculdade")
_add([5942, 5943], "Educação", "Livros e Material")
_add([6300], "Seguros", "Outros Seguros")
_add([9311], "Impostos", "Outros Impostos")
_add([5734, 7371, 7372, 7373, 7379], "Trabalho e Negócio", "Ferramentas e Software")
_add([7311], "Trabalho e Negócio", "Marketing e Escritório")
_add([8931], "Trabalho e Negócio", "Contabilidade e Serviços")
_add([5947, 5992], "Presentes e Doações", "Presentes")
_add([8398], "Presentes e Doações", "Doações")


def por_mcc(mcc) -> tuple[str, str] | None:
    """(categoria, subcategoria) para o MCC, ou None se ausente/ambíguo."""
    if mcc is None:
        return None
    return _TAB.get(str(mcc).strip())
