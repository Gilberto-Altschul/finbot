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
from app.utils import _fmt, _normalize

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
- Sempre informe o total acumulado (da categoria para gastos ou total do mês para receitas) após um registro
- SAÚDE vs FINANCEIRO: Planos de Saúde ou Convênios DEVEM ser registrados na categoria 'Saúde'. A categoria 'Financeiro' é apenas para taxas e seguros de bens (carro/casa).
"""

ONBOARDING_MSG = (
    "👋 Olá! Sou o *FinBot*, seu assistente de controle financeiro.\n\n"
    "Para começar, me diz: *qual o dia de vencimento da sua fatura do cartão de crédito?*\n\n"
    "_(Se não usar cartão, pode digitar qualquer dia — ex: 1)_"
)

_EXPENSE_RE = re.compile(
    r"^(?P<desc>[a-zA-ZÀ-ÿ0-9 ]+?)\s+(?:(?P<parcelas_pre>\d+)[xX]\s+)?(?P<valor>\d+(?:[.,]\d{1,2})?)(?:\s+(?P<parcelas_pos>\d+)[xX])?(?:\s+(?P<data>\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?))?(?:\s+(?P<method>cr[ée]d(?:ito)?|cart[ãa]o|d[ée]b(?:ito)?|dinheiro))?(?:\s+(?:para|pro|pra)\s+(?P<beneficiario>.+))?$",
    re.IGNORECASE,
)

_KEYWORD_TOOLS = {
    # --- AUDITORIA E EXTRATOS ---
    "extrato": "listar_gastos_detalhados",
    "lista": "listar_gastos_detalhados",
    "listar": "listar_gastos_detalhados",
    "acertar": "processar_comando_acerto", # Comando novo que criamos
    "gastos": "listar_categoria",
    "gasto": "listar_categoria",
    
   
    # --- FINANCEIRO & SALDO ---
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
            resumo = f"✅ *Gasto registrado!* \n💰 R$ {_fmt(result['valor'])} em {result['descricao']}"
            if "total_categoria_mes" in result:
                resumo += f"\n📊 Total {result['categoria']} no mês: R$ {_fmt(result['total_categoria_mes'])}"
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
        return "💡 *FinBot — O que você pode perguntar:*\n\n*📊 VER GASTOS*\n• _'resumo'_\n• _'gastos alimentação'_\n• _'maiores gastos'_\n• _'essa semana'_\n• _'mês passado'_\n\n*💳 CARTÃO E CONTA*\n• _'fatura'_\n• _'parcelamentos'_\n• _'saldo'_"
    if tool_name == "direct_reply":
        return args["mensagem"]
    result = await tool_registry.execute(tool_name, args, user_phone)
    return _format_output(result, tool_name, user_phone)

async def _classify(message: str, user_phone: str) -> dict | None:
    msg = message.strip()
    msg_norm = _normalize(msg)

    # 0. Contexto: Resposta à pergunta de ambiguidade (Alimentação vs Lazer)
    # Se o usuário respondeu apenas a categoria após a nossa pergunta, recuperamos o gasto original.
    if msg_norm in ["alimentacao", "lazer", "refeicao comum", "saida social"]:
        history = db.get_history(user_phone, limit=2)
        if history and len(history) >= 1:
            last_reply = history[-1]
            # Verifica se a última mensagem do assistente foi a pergunta de ambiguidade
            if last_reply["role"] == "assistant" and "Este gasto foi *Alimentação*" in last_reply["content"]:
                # Recupera a mensagem do usuário que gerou a dúvida (ex: "cafe 8")
                if len(history) >= 2:
                    prev_user_msg = history[-2]["content"]
                    m_prev = _EXPENSE_RE.match(prev_user_msg)
                    if m_prev:
                        desc_raw = m_prev.group("desc").strip()
                        valor = float(m_prev.group("valor").replace(",", "."))
                        method = _parse_method(m_prev.group("method"))
                        cat_escolhida = "Alimentação" if "alimentacao" in msg_norm or "refeicao" in msg_norm else "Lazer"
                        
                        return {
                            "tool": "registrar_gasto",
                            "args": {"valor": valor, "categoria": cat_escolhida, "subcategoria": desc_raw.capitalize(), "descricao": desc_raw, "payment_method": method}
                        }

    # 1. PRIORIDADE MÁXIMA: Paginação (Ex: "listar 5 pag 2")
    if "listar" in msg_norm and "pag" in msg_norm:
        m_pag = re.search(r"pag (\d+)", msg_norm)
        pagina = int(m_pag.group(1)) if m_pag else 1
        
        m_mes = re.search(r"listar (\d+)", msg_norm)
        mes = m_mes.group(1) if m_mes else str(date.today().month)
        
        return {"tool": "listar_gastos_detalhados", "args": {"mes": mes, "pagina": pagina}}

    # 2. Comando "Acertar"
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

    # 3. Detecção de Mês por extenso (Evita chamadas desnecessárias à LLM)
    meses_map = {
        "janeiro": "01", "fevereiro": "02", "marco": "03", "abril": "04",
        "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
        "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12"
    }
    target_month = None
    for m_nome, m_num in meses_map.items():
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
                if any(k in msg_norm for k in ["extrato", "lista", "listar"]):
                    return {"tool": "listar_gastos_detalhados", "args": {"mes": target_month or str(date.today().month), "pagina": 1}}

                for cat in ["Alimentação", "Transporte", "Moradia", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets"]:
                    if _normalize(cat) in msg_norm:
                        args["categoria"] = cat
                        return {"tool": tool_name, "args": args}
                
                # Se digitar 'gastos' sem categoria (ex: 'gastos maio'), vai para o detalhado
                return {"tool": "listar_gastos_detalhados", "args": args}
            
            if tool_name == "consultar_limite":
                return {"tool": "consultar_limite", "args": {}}
            
            return {"tool": tool_name, "args": args}
        
    # Caso residual para listagem básica
    if any(k in msg_norm for k in ["extrato", "lista", "listar"]):
        return {"tool": "listar_gastos_detalhados", "args": {"mes": target_month or str(date.today().month), "pagina": 1}}

    m = _EXPENSE_RE.match(msg)
    if m:
        desc_raw = m.group("desc").strip()
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
        if cat == "Perguntar":
            # Interceptamos a ambiguidade aqui para evitar a chamada desnecessária de LLM
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
            logger.warning(f"Fallback para LLM devido a falha no Fast Path: {exc}")

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
                    if "category" in args_pt: args_pt["categoria"] = args_pt.pop("category")
                    if "description" in args_pt: args_pt["descricao"] = args_pt.pop("description")
                    
                    if call["name"] == "registrar_gasto" and "subcategoria" not in args_pt:
                        c, s = await categorizar_gasto_hibrido(user_phone, args_pt.get("descricao", user_message))
                        if c != "Perguntar":
                            args_pt["categoria"] = c
                            args_pt["subcategoria"] = s
                        
                    result = await tool_registry.execute(call["name"], args_pt, user_phone)
                    
                    if call["name"] == "diagnosticar_estouro":
                        try:
                            logger.info("Injetando dados estruturados reais para a LLM...")
                            resposta_ia = await call_llm(
                                system=SYSTEM,
                                history=history + [{"role": "user", "content": user_message}],
                                message=f"Aqui estão os dados reais extraídos do banco de dados: {json.dumps(result, ensure_ascii=False)}. Com base unicamente nesses dados, formule a resposta visual e amigável para o WhatsApp explicando detalhadamente os motivos do estouro orçamentário.",
                                tools=[]
                            )
                            reply = resposta_ia["content"]
                        except Exception as inner_exc:
                            logger.warning(f"Erro ao processar insight humano (Cota Excedida?). Usando fallback nativo: {inner_exc}")
                            reply = result.get("mensagem", "Não consegui gerar o diagnóstico detalhado agora.")
                        
                        db.save_message(user_phone, "assistant", reply)
                        return reply
                    
                    tool_results.append(_format_output(result, call["name"], user_phone))
                except Exception as exc:
                    tool_results.append(f"[{call['name']}] ERRO: {exc}")

            texto_consolidado = "\n".join(tool_results)
            if len(texto_consolidado) > 1500:
                count_sucesso = len([r for r in tool_results if "✅" in r or "💰" in r])
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
            categoria_detectada = "Alimentação"
            for cat in ["Transporte", "Moradia", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets"]:
                if _normalize(cat) in msg_norm:
                    categoria_detectada = cat
                    break
            result_raw = await tool_registry.execute("diagnosticar_estouro", {"categoria": categoria_detectada}, user_phone)
            reply = result_raw.get("mensagem", "Cota de IA excedida. Tente novamente mais tarde.")
        elif "gastos" in msg_norm:
            # Se a IA esgotar e você pedir gastos, ele intercepta aqui e força o caminho matemático puro!
            categoria_detectada = None
            for cat in ["Transporte", "Moradia", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets"]:
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