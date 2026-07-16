# app/agent.py
# ─────────────────────────────────────────────────────────────────────────────
# FinBot Agent — o cérebro do sistema (Versão Unificada, Corrigida e Produção)
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any
from datetime import date, datetime

import app.database as db
from app.categorizer import categorizar_gasto_hibrido
import app.tools as tool_registry
from app.llm import call_llm
from app.utils import _fmt, _normalize, SISTEMA_CATEGORIAS
from app.pluggy_service import PluggyService
from app.config import get_settings

settings = get_settings()
pluggy_service = PluggyService()

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM = """
Você é o FinBot, um assistente financeiro pessoal via WhatsApp.
Seu objetivo: ajudar o usuário a registrar gastos, receitas (incomes) e entender sua vida financeira.

PERSONALIDADE:
- Direto, amigável e sem enrolação (é WhatsApp, não email)
- Use emojis com moderação para deixar as mensagens mais legíveis
- Responda sempre em português do Brasil

REGRAS IMPORTANTES:
- Registre gastos SEM pedir confirmação — o usuário quer agilidade
- EXCEÇÃO CRÍTICA: Se o local for um Restaurante, Bar, Café ou Padaria, você NÃO PODE registrar direto. Pergunte: "Este gasto foi Alimentação (refeição comum) ou Lazer (saída social)?"
- Sempre informe o total acumulado (da categoria para gastos ou total do mês para receitas) após um registro.
- COMPARAÇÕES: Se o usuário solicitar uma comparação entre períodos (ex: "compara maio e junho"), você DEVE chamar as ferramentas (como resumo_mensal ou listar_gastos_detalhados) para *TODOS* os períodos mencionados *em uma única resposta contendo múltiplas chamadas de ferramentas*. Sempre passe o argumento 'mes' no formato 'YYYY-MM' e argumentos de dia (dia_inicio/dia_fim) APENAS como números inteiros. NUNCA pergunte se deve buscar os dados do segundo período; assuma que sim e proceda com a coleta de todas as informações necessárias para a comparação.
- QUANDO / DATAS: Se o usuário perguntar quando algo foi pago ou o dia de um gasto, use 'listar_categoria' (se souber a categoria) ou 'listar_gastos_detalhados' para encontrar a data exata nos dados retornados.
- SAÚDE vs FINANCEIRO: Planos de Saúde ou Convênios DEVEM ser registrados na categoria 'Saúde'. A categoria 'Financeiro' é para taxas e seguros de bens (vida/casa). Seguros de automóvel pertencem à categoria 'Transporte'.

CATEGORIAS E SUBCATEGORIAS:
- Moradia: Aluguel, Condomínio, Contas, Celular, Faxina, Manutenção Residencial, Utensílios
- Alimentação: Delivery, Mercado, Restaurante, Padaria, Lanche, Café
- Transporte: Aplicativo, Combustível, Estacionamento, Ônibus, Metrô, Oficina, Manutenção Veículo, Seguro Automóvel
- Saúde: Farmácia, Academia, Médico, Dentista, Suplemento, Exame, Plano de Saúde, Convênio
- Lazer: Streaming, Cinema, Show, Viagem, Bar, Balada, Presente
- Vestuário e Beleza: Roupa, Calçado, Cabelo, Barbearia, Manicure, Estética
- Educação: Curso, Livro, Faculdade, Software
- Financeiro: Seguro (Vida/Residencial), Tarifa, Anuidade, Imposto, Taxa
- Pets: Ração, Veterinário, Petshop, Banho, Tosa
- Família e Dependentes: Mesada, Pensão, Apoio Familiar, Presente Familiar, Emergência Familiar, Empréstimo Pessoal
- Empresa: MEI, Impostos PJ, Escritório, Marketing, Pró-labore, Ferramentas
"""

ONBOARDING_MSG = (
    "👋 Olá! Sou o *FinBot*, seu assistente de controle financeiro.\n\n"
    "Para começar, me diz: *qual o dia de vencimento da sua fatura do cartão de crédito?*\n\n"
    "_(Se não usar cartão, pode digitar qualquer dia — ex: 1)_"
)

_EXPENSE_RE = re.compile(
    r"^(?P<desc>[\wÀ-ÿ&.*-]+(?:\s+[\wÀ-ÿ&.*-]+)*?)\s+(?:(?P<parcelas_pre>\d+)[xX]\s+)?(?P<valor>\d+(?:[.,]\d{1,2})?)(?:\s+(?P<parcelas_pos>\d+)[xX])?(?:\s+(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?))?(?:\s+(?P<method>cr[ée]d(?:ito)?|cart[ãa]o|d[ée]b(?:ito)?|dinheiro))?(?:\s+(?:para|pro|pra)\s+(?P<beneficiario>.+))?$",
    re.IGNORECASE,
)

_KEYWORD_TOOLS = {
    # --- AUDITORIA E EXTRATOS ---
    "extrato": "listar_gastos_detalhados",
    "lista": "listar_gastos_detalhados",
    "listar": "listar_gastos_detalhados",
    "ver": "listar_gastos_detalhados",
    "acertar": "processar_comando_acerto", # Comando novo que criamos
    "categorias": "listar_menu_categorias",
    "gastos": "listar_categoria",
    "gasto": "listar_categoria",
    
   
    # --- FINANCEIRO & SALDO ---
    "sincronizar": "sincronizar_banco",
    "sync": "sincronizar_banco",

    "saldo": "consultar_saldo",
    "receitas": "listar_receitas",
    "fatura": "consultar_fatura",
    "cartao": "consultar_fatura",
    "cartão": "consultar_fatura",
    
    # --- ORÇAMENTOS E LIMITES ---
    "limite": "consultar_limite",
    "limites": "consultar_limite",
    "orçamento": "consultar_limite",
    "orcamento": "consultar_limite",
    "meu limite": "consultar_limite",
    "meus limites": "consultar_limite",
    
    # --- ANÁLISE E RESUMOS ---
    "resumo": "resumo_mensal",
    "maiores gastos": "maiores_gastos",
    "tendencia": "tendencia_semanal",
    "essa semana": "tendencia_semanal",
    
    # --- UTILITÁRIOS ---
    "ajuda": "ajuda",
    "parcelamentos": "listar_parcelamentos"
}

_MESES_MAP = {
    "janeiro": "01", "fevereiro": "02", "marco": "03", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12"
}

def _parse_method(raw: str | None) -> str:
    if not raw: return "debito"
    raw = raw.lower().strip()
    if any(w in raw for w in ["créd", "cred", "cartão", "cartao"]): return "credito"
    return "debito"

def _format_output(result: Any, tool_name: str, user_phone: str) -> str:
    if not isinstance(result, dict): return str(result)
    if result.get("erro"): return f"⚠️ {result['erro']}"

    if tool_name == "listar_categoria":
        return result.get("mensagem", "Não encontrei gastos para essa categoria.")

    if tool_name == "registrar_gasto":
        if result.get("registrado"):
            val = result.get("valor") or result.get("valor_total")
            desc = result.get("descricao") or result.get("description", "Gasto")
            
            if result.get("tipo") == "parcelado" and "detalhes" in result:
                resumo = f"✅ *Gasto parcelado registrado!* \n💰 Total R$ {_fmt(val)} em {desc}\n"
                cat = result.get("categoria", "Gasto")
                for d in result["detalhes"]:
                    resumo += f"\n💳 *{d['fatura_label']}*: Parcela R$ {_fmt(d['valor_parcela'])}"
                    resumo += f"\n📊 Total {cat}: R$ {_fmt(d['total_categoria_fatura'])}\n"
                return resumo

            fatura = f" ({result['fatura_label']})" if result.get("fatura_label") else ""
            resumo = f"✅ *Gasto registrado!* \n💰 R$ {_fmt(val)} em {desc}{fatura}"
            if "total_categoria_mes" in result:
                cat_name = result.get("categoria") or "Gasto"
                resumo += f"\n📊 Total {cat_name} no mês: R$ {_fmt(result['total_categoria_mes'])}"
            return resumo

    if tool_name == "registrar_receita":
        resumo = f"💰 *R$ {_fmt(result['valor'])}* recebido com sucesso!\n📝 {result['descricao']}"
        if "total_receitas_mes" in result:
            resumo += f"\n📊 Total de receitas no mês: R$ {_fmt(result['total_receitas_mes'])}"
        return resumo

    if isinstance(result, dict) and "mensagem" in result:
        return result["mensagem"]

    return json.dumps(result, ensure_ascii=False)

async def _fast_path(tool_name: str, args: dict, user_phone: str) -> str:
    if tool_name == "ajuda":
        return "💡 *FinBot — O que você pode perguntar:*\n\n*📊 VER GASTOS*\n• _'resumo'_\n• _'gastos alimentação'_\n• _'maiores gastos'_\n• _'essa semana'_\n• _'mês passado'_\n\n*💳 CARTÃO E CONTA*\n• _'fatura'_\n• _'parcelamentos'_\n• _'saldo'_\n\n*📁 CONFIGURAÇÃO*\n• _'categorias'_"
    
    if tool_name == "listar_menu_categorias":
        return (
            "📁 *Guia de Categorias e Exemplos (Keywords)*\n"
            "Use esses termos para facilitar a organização:\n\n"
            "🏠 *Moradia:* Aluguel (QuintoAndar), Condomínio, Energia (Enel), Água, Gás, Internet (Claro/Vivo), Celular, Faxina, Reforma (Leroy), Mobília\n\n"
            "🍔 *Alimentação:* Mercado (Assaí/Extra), Feira, Delivery (iFood/Rappi), Restaurante, Padaria, Café, Bar\n"
            "_(⚠️ Em restaurantes/bares eu pergunto se é social ou dia a dia!)_\n\n"
            "🚗 *Transporte:* Apps (Uber/99), Combustível (Posto), Público (Metrô), Estacionamento, Manutenção, Passagem Aérea\n\n"
            "💊 *Saúde:* Plano (Unimed/Amil), Farmácia (Raia), Médico/Exame, Academia (Smartfit), Terapia, Nutrição (Whey)\n\n"
            "🎬 *Lazer:* Streaming (Netflix), Cinema/Shows, Viagem (Airbnb/Booking), Balada, Jogos (Steam), Restaurante Social\n\n"
            "👕 *Vestuário/Beleza:* Roupas (Zara/Renner), Calçados (Nike), Salão/Barbearia, Cosméticos, Presentes\n\n"
            "📚 *Educação:* Escola, Cursos Online (Udemy/Alura), Material, Idiomas\n\n"
            "💳 *Financeiro:* Empréstimo, Seguros (Porto), Investimento (XP/BTG), Impostos (IPTU/IPVA), Tarifas, Apostas (Bets)\n\n"
            "🐾 *Pets:* Ração (Petz/Cobasi), Veterinário, Petshop, Plano Pet\n\n"
            "👨‍👩‍👧 *Família:* Mesada, Pensão, Apoio Familiar, Presente, Emergência\n\n"
            "💼 *Empresa:* MEI, Impostos PJ, Escritório, Marketing, Pró-labore\n\n"
            "💰 *Receitas:* Salário, Freela, Rendimento, Reembolso"
        )

    if tool_name == "direct_reply":
        return args["mensagem"]
    result = await tool_registry.execute(tool_name, args, user_phone)

    if tool_name == "sincronizar_banco":
        return result.get("mensagem", "❌ Não consegui sincronizar com a Pluggy.")
    
    return _format_output(result, tool_name, user_phone)

async def _classify(message: str, user_phone: str) -> dict | None:
    msg_norm = _normalize(user_message)
# Se o usuário digitou um número e existe uma sessão ativa
    if user_phone in tool_registry._SESSAO_LISTAGEM and msg_norm.isdigit():
        idx = int(msg_norm) - 1
        opcoes = tool_registry._SESSAO_LISTAGEM[user_phone]
        
        if 0 <= idx < len(opcoes):
            account_id = opcoes[idx]["account_id"]
            # Limpa a sessão
            del tool_registry._SESSAO_LISTAGEM[user_phone]
            # Executa a sincronização diretamente
            return await tool_registry.execute("sincronizar_banco", {"account_id": account_id}, user_phone)
        else:
            return "⚠️ Opção inválida. Digite apenas o número da lista."

    # Remove timestamps e metadados de mensagens coladas do WhatsApp (ex: [6:09 PM] Gilberto:)
    msg = re.sub(r"\[.*\]\s+.*:\s+", "", message).strip()
    msg_norm = _normalize(msg)

    # No app/agent.py, na lógica de comando 'sincronizar'
    if msg_norm.startswith("sincronizar"):
        partes = msg_norm.split()
        
        if len(partes) < 3:
            # Força o usuário a fornecer os dois parâmetros
            reply = "⚠️ Formato obrigatório: `sincronizar [ITEM_ID] [ACCOUNT_ID]`"
        else:
            item_id = partes[1]
            account_id = partes[2]
            # Chamada para o novo método que filtra
            reply = await tool_registry.sincronizar_banco_especifico(user_phone, item_id, account_id)

        return {"tool": "direct_reply", "args": {"mensagem": reply}}
            
    # Paginação (Ex: "listar 5 pag 2")
    if "listar" in msg_norm and "pag" in msg_norm:
        m_pag = re.search(r"pag (\d+)", msg_norm)
        pagina = int(m_pag.group(1)) if m_pag else 1
        
        m_mes = re.search(r"listar (\d+)", msg_norm)
        mes = m_mes.group(1) if m_mes else str(date.today().month)
        
        return {"tool": "listar_gastos_detalhados", "args": {"mes": mes, "pagina": pagina}}

    # Comando "Acertar" (Ex: "acertar 1 excluir")
    if msg_norm.startswith("acertar"):
        parts = msg_norm.split()
        try:
            if len(parts) >= 3:
                return {
                 "tool": "processar_comando_acerto", 
                 "args": {"indice": int(parts[1]), "acao": parts[2], "valor": " ".join(parts[3:])}
                }
        except (ValueError, IndexError):
            pass

    # 2. Gasto Manual (Fast Path) - Detecta 'cafe 1' ou 'Almoço 35.50'
    m = _EXPENSE_RE.match(msg)
    if m:
        desc_raw = m.group("desc").strip()
        # Evita capturar comandos conhecidos como se fossem nomes de estabelecimentos
        if desc_raw.lower() not in ["acertar", "listar", "extrato", "ver", "lista", "ajuda", "saldo", "fatura", "cartao", "limite"]:
            desc_norm = _normalize(desc_raw)
            valor = float(m.group("valor").replace(",", "."))
            method = _parse_method(m.group("method"))
            parc = int(m.group("parcelas_pre") or m.group("parcelas_pos") or 1)
            data_raw = m.group("data")
            
            # Identifica se é uma receita (income) em vez de gasto
            if any(k in desc_norm for k in ["salario", "salário", "receita", "reembolso", "rendimento"]):
                cat_receita = "Salário" if "salario" in desc_norm else "Extra"
                if "reembolso" in desc_norm: cat_receita = "Reembolso"
                if "rendimento" in desc_norm: cat_receita = "Investimento"
                
                args_receita = {"valor": valor, "categoria": cat_receita, "descricao": desc_raw}
                if data_raw:
                    try:
                        parts = re.split(r"[/.-]", data_raw)
                        d, mo = int(parts[0]), int(parts[1])
                        y = int(parts[2]) if len(parts) > 2 else date.today().year
                        if y < 100: y += 2000
                        args_receita["data"] = f"{y}-{mo:02d}-{d:02d}"
                    except: pass
                return {"tool": "registrar_receita", "args": args_receita}

            cat, sub = await categorizar_gasto_hibrido(user_phone, desc_raw)
            if cat != "Perguntar" and cat not in SISTEMA_CATEGORIAS:
                cat = "Outros"

            if cat == "Perguntar":
                return {
                    "tool": "direct_reply", 
                    "args": {"mensagem": f"Hum, vi que você digitou '*{desc_raw}*'.\n\nEste gasto foi *Alimentação* (refeição comum) ou *Lazer* (saída social)?"}
                }
                
            args = {"valor": valor, "categoria": cat, "subcategoria": sub, "descricao": desc_raw, "payment_method": method}
            if data_raw:
                try:
                    parts = re.split(r"[/.-]", data_raw)
                    d, mo = int(parts[0]), int(parts[1])
                    y = int(parts[2]) if len(parts) > 2 else date.today().year
                    if y < 100: y += 2000
                    args["data"] = f"{y}-{mo:02d}-{d:02d}"
                except: pass
            if method == "credito" and parc > 1: args["parcelas"] = parc
            return {"tool": "registrar_gasto", "args": args}

    # 0. DEFINIR LIMITE — PRIORIDADE ABSOLUTA (Fast Path para evitar alucinações da IA)
    if "limite" in msg_norm and (any(x in msg_norm for x in ["definir", "estipular", "setar", "atualizar", "mudar", "alterar", "novo", "para", "pra"]) or re.search(r"\d", msg_norm)):
        # Regex aprimorada: captura sequências numéricas completas com suporte a padrão BR
        val_match = re.search(r"(\d+(?:[.,]\d+)*)", msg_norm)
        if val_match:
            try:
                raw_val = val_match.group(1)
                # Inteligência de parsing para 3.970,00 ou 3970,00 ou 3970
                if "." in raw_val and "," in raw_val:
                    # Formato 1.234,56
                    raw_val = raw_val.replace(".", "").replace(",", ".")
                elif "," in raw_val:
                    # Formato 1234,56
                    raw_val = raw_val.replace(",", ".")
                elif "." in raw_val:
                    # Caso ambíguo: 1.000 ou 1000.00?
                    # Se houver apenas um ponto e não houver exatamente 2 casas decimais depois, assume milhar
                    parts = raw_val.split(".")
                    if len(parts[-1]) != 2:
                        raw_val = raw_val.replace(".", "")
                
                valor_limit = float(raw_val)
                
                cat_final = "Outros"
                for c in SISTEMA_CATEGORIAS:
                    c_norm = _normalize(c)
                    # Melhora a detecção para categorias com nomes longos (ex: família detecta Família e Dependentes)
                    if c_norm in msg_norm or any(part in msg_norm and len(part) > 3 for part in c_norm.split()):
                        cat_final = c
                        break
                
                # Identifica o mês de referência
                mes_ref = date.today().strftime("%Y-%m")
                for m_nome, m_num in _MESES_MAP.items():
                    if m_nome in msg_norm:
                        mes_ref = f"{date.today().year}-{m_num}"
                        break
                
                logger.info(f"Fast Path Limite detectado: {cat_final} | {valor_limit} | {mes_ref}")
                return {"tool": "definir_limite", "args": {"categoria": cat_final, "valor": valor_limit, "mes": mes_ref}}
            except Exception as e:
                logger.error(f"Erro no parsing de limite: {e}")

    # 0.5 INTENÇÕES COMPLEXAS: Se o usuário quer comparar, analisar ou saber motivos, ignora Fast Path e vai para LLM
    if any(k in msg_norm for k in ["compara", "diferenca", "analise", "evolucao", "porque", "por que", "dias", "primeiros", "ultimos", "quando", "que dia", "zerado", "vazio", "errado"]):
        return None
    
    # 0. Contexto: Resposta à pergunta de ambiguidade (Alimentação vs Lazer)
    # Se o usuário respondeu apenas a categoria após a nossa pergunta, recuperamos o gasto original.
    intent_alimentacao = any(x in msg_norm for x in ["alimentacao", "refeicao", "comum", "dia a dia"])
    intent_lazer = any(x in msg_norm for x in ["lazer", "social", "saida", "especial"])

    if intent_alimentacao or intent_lazer:
        history = db.get_history(user_phone, limit=2)
        if history:
            last_reply = history[-1]
            # Verifica se a última mensagem do assistente foi a pergunta de ambiguidade, ignorando formatação
            content_norm = _normalize(last_reply.get("content", ""))
            if last_reply["role"] == "assistant" and "este gasto foi" in content_norm and ("alimentacao" in content_norm or "lazer" in content_norm):
                # Recupera a mensagem do usuário que gerou a dúvida (ex: "cafe 8")
                if len(history) >= 2:
                    # Também limpamos o timestamp da mensagem anterior do histórico se necessário
                    prev_user_msg = re.sub(r"\[.*\]\s+.*:\s+", "", history[-2]["content"]).strip()
                    m_prev = _EXPENSE_RE.match(prev_user_msg)
                    if m_prev:
                        desc_raw = m_prev.group("desc").strip()
                        valor = float(m_prev.group("valor").replace(",", "."))
                        method = _parse_method(m_prev.group("method"))
                        cat_escolhida = "Alimentação" if intent_alimentacao else "Lazer"
                        
                        return {
                            "tool": "registrar_gasto",
                            "args": {"valor": valor, "categoria": cat_escolhida, "subcategoria": desc_raw.capitalize(), "descricao": desc_raw, "payment_method": method}
                        }

    # 3. Detecção de Mês por extenso (Evita chamadas desnecessárias à LLM)
    target_month = None
    for m_nome, m_num in _MESES_MAP.items():
        if m_nome in msg_norm:
            target_month = f"{date.today().year}-{m_num}"
            break

    # 4. Roteamento de Ferramentas via Keywords
    for keyword, tool_name in _KEYWORD_TOOLS.items():
        if _normalize(keyword) in msg_norm:
            # Prepara argumentos base (injetando o mês se detectado)
            args = {"mes": target_month} if target_month else {}

            if tool_name == "listar_categoria":
                # Se o usuário quer explicitamente 'extrato' ou 'lista', pula categoria
                if any(k in msg_norm for k in ["extrato", "lista", "listar", "ver"]):
                    return {"tool": "listar_gastos_detalhados", "args": {"mes": target_month or str(date.today().month), "pagina": 1}}

                for cat in SISTEMA_CATEGORIAS:
                    if _normalize(cat) in msg_norm:
                        args["categoria"] = cat
                        return {"tool": tool_name, "args": args}
                
                # Se digitar 'gastos' sem categoria (ex: 'gastos maio'), vai para o detalhado
                return {"tool": "listar_gastos_detalhados", "args": args}
            
            if tool_name == "consultar_limite":
                return {"tool": "consultar_limite", "args": {}}
            
            return {"tool": tool_name, "args": args}
        
    # Caso residual para listagem básica
    if any(k in msg_norm for k in ["extrato", "lista", "listar", "ver"]):
        return {"tool": "listar_gastos_detalhados", "args": {"mes": target_month or str(date.today().month), "pagina": 1}}

    return None


async def run(user_phone: str, user_message: str) -> str:
    logger.info(f"Agent run para {user_phone}")
    
    classified = await _classify(user_message, user_phone)
    if classified:
        try:
            reply = await _fast_path(classified["tool"], classified["args"], user_phone)
            db.save_message(user_phone, "user", user_message)
            db.save_message(user_phone, "assistant", reply)
            return reply
        except Exception as exc:
            logger.error(f"Falha técnica na execução do comando: {exc}")
            return f"❌ Tive um problema técnico ao executar esse comando: {exc}. Por favor, tente novamente em instantes."

    if db.is_new_user(user_phone):
        db.save_message(user_phone, "user", user_message)
        db.save_message(user_phone, "assistant", ONBOARDING_MSG)
        return ONBOARDING_MSG

    history = db.get_history(user_phone)
    db.save_message(user_phone, "user", user_message)

    try:
        response = await call_llm(system=SYSTEM, history=history, message=user_message, tools=tool_registry.SCHEMAS)
        if response["type"] == "tool_call":
            tool_results = []
            for call in response["tool_calls"]:
                try:
                    args_pt = dict(call["args"])
                    if "amount" in args_pt: args_pt["valor"] = args_pt.pop("amount")
                    if "category" in args_pt: args_pt["categoria"] = args_pt.pop("category")
                    if "description" in args_pt: args_pt["descricao"] = args_pt.pop("description")
                    
                    if call["name"] == "registrar_gasto":
                        # Força o uso do categorizador híbrido para garantir que as regras de proteção 
                        # (como Família e Saúde) prevaleçam sobre a intuição da LLM.
                        c, s = await categorizar_gasto_hibrido(user_phone, args_pt.get("descricao", user_message))
                        if c != "Perguntar":
                            if c not in SISTEMA_CATEGORIAS: c = "Outros"
                            args_pt["categoria"] = c
                            args_pt["subcategoria"] = s
                        
                    result = await tool_registry.execute(call["name"], args_pt, user_phone)
                    # Armazena os dados para síntese inteligente
                    tool_results.append({"name": call["name"], "data": result})
                except Exception as exc:
                    tool_results.append({"name": call["name"], "data": {"erro": str(exc)}})

            # Verifica se precisamos de uma síntese inteligente
            # 1. Intenção explícita na mensagem atual
            intent_analitica = any(k in _normalize(user_message) for k in ["compara", "diferenca", "analise", "evolucao", "porque", "por que", "quando", "que dia", "dias", "primeiros", "ultimos", "tendencia", "zerado", "vazio", "errado"])
            
            # 2. Contexto: se o usuário deu uma resposta curta a um processo analítico anterior
            ferramentas_leitura = {"resumo_mensal", "listar_categoria", "listar_gastos_detalhados", "maiores_gastos", "tendencia_semanal", "consultar_fatura"}
            chamou_leitura = any(tr["name"] in ferramentas_leitura for tr in tool_results)
            if not intent_analitica and chamou_leitura and len(user_message) <= 10:
                msg_norm = _normalize(user_message)
                if msg_norm in ["sim", "ok", "pode", "manda", "va", "vai", "claro"]:
                    contexto_recente = " ".join([m["content"] for m in history[-2:]])
                    intent_analitica = any(k in _normalize(contexto_recente) for k in ["compara", "diferenca", "analise", "evolucao"])

            tem_diagnostico = any(tr["name"] == "diagnosticar_estouro" for tr in tool_results)

            if intent_analitica or tem_diagnostico:
                try:
                    logger.info("Sintetizando resultados analíticos via LLM...")
                    prompt_sintese = f"O usuário solicitou uma análise ou continuação de comparação. Dados brutos retornados: {json.dumps([tr['data'] for tr in tool_results], ensure_ascii=False)}. Formule uma resposta humana, direta e comparativa com base nesses dados e no histórico da conversa."
                    resposta_ia = await call_llm(
                        system=SYSTEM,
                        history=history + [{"role": "user", "content": user_message}],
                        message=prompt_sintese,
                        tools=[]
                    )
                    reply = resposta_ia["content"]
                except Exception as e:
                    logger.error(f"Erro na síntese analítica: {e}")
                    reply = "\n\n".join([_format_output(tr["data"], tr["name"], user_phone) for tr in tool_results])
            else:
                # Comportamento padrão: apenas concatena as respostas formatadas
                saidas = [_format_output(tr["data"], tr["name"], user_phone) for tr in tool_results]
                texto_consolidado = "\n".join(saidas)
                if len(texto_consolidado) > 1500:
                    count_sucesso = len([r for r in saidas if "✅" in r or "💰" in r])
                    reply = f"✨ *Sincronização Concluída!*\n\n📊 Processados *{count_sucesso} novos lançamentos*."
                else:
                    reply = texto_consolidado

            db.save_message(user_phone, "assistant", reply)
            return reply
        else:
            reply = response["content"]
            
    except Exception as exc:
        # CORREÇÃO CRÍTICA DE COLO: Adicionado tratamento estático para "gastos" no Fallback Diário
        logger.error(f"Erro no loop agêntico principal (Cota esgotada Gemini): {exc}")
        
        msg_norm = _normalize(user_message)
        if "por que" in msg_norm or "estourou" in msg_norm or "motivo" in msg_norm:
            categoria_detectada = "Outros"
            for cat in SISTEMA_CATEGORIAS:
                if _normalize(cat) in msg_norm:
                    categoria_detectada = cat
                    break
            result_raw = await tool_registry.execute("diagnosticar_estouro", {"categoria": categoria_detectada}, user_phone)
            reply = result_raw.get("mensagem", "Cota de IA excedida. Tente novamente mais tarde.")
        elif "gastos" in msg_norm:
            # Se a IA esgotar e você pedir gastos, ele intercepta aqui e força o caminho matemático puro!
            categoria_detectada = None
            for cat in SISTEMA_CATEGORIAS:
                if _normalize(cat) in msg_norm:
                    categoria_detectada = cat
                    break
            if categoria_detectada:
                reply = await _fast_path("listar_categoria", {"categoria": categoria_detectada}, user_phone)
            else:
                reply = await _fast_path("listar_gastos_detalhados", {}, user_phone)
        elif "resumo" in msg_norm:
            reply = await _fast_path("resumo_mensal", {}, user_phone)
        else:
            reply = "⚠️ *Aviso do FinBot:* Minha cota diária de inteligência artificial gratuita foi atingida para hoje.\n\nMas você ainda pode registrar gastos digitando no padrão natural! Ex: `Padaria 15,90`"

    db.save_message(user_phone, "assistant", reply)
    return reply