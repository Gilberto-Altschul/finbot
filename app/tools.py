# app/tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Recursos Executáveis do FinBot — Versão Unificada e Homologada em Produção
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import re
import logging
import unicodedata
from calendar import monthrange
from datetime import date, datetime, time
from typing import Any

import app.database as db
from app.billing import fatura_vencimento, fatura_label, parcelas
from app.utils import _fmt, _normalize, SISTEMA_CATEGORIAS
from app.categorizer import categorizar_gasto_hibrido

logger = logging.getLogger(__name__)

def _fmt_moeda(v: float) -> str: return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
def _fmt_inteiro(v: float) -> str: return f"{round(v):,}".replace(",", ".")

# Variável global para manter o estado da listagem por usuário (IDs das transações exibidas)
_SESSAO_LISTAGEM = {}

# ── Tool schemas ──────────────────────────────────────────────────────────────

SCHEMAS: list[dict] = [
    {
        "name": "sincronizar_banco",
        "description": "Busca transações automáticas via Open Finance (Pluggy).",
        "parameters": {
            "type": "object",
            "properties": {
                "arquivo": {"type": "string"},
                "account_id": {"type": "string"}
            }
        }
    },
    {
        "name": "registrar_gasto",
        "description": "Registra um gasto do usuário. Use sempre que houver despesa, compra ou pagamento.",
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {"type": "number"},
                "categoria": {"type": "string", "enum": ["Moradia", "Alimentação", "Transporte", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets", "Empresa", "Família e Dependentes"]},
                "subcategoria": {"type": "string"},
                "descricao": {"type": "string"},
                "beneficiario": {"type": "string"},
                "data": {"type": "string"},
                "payment_method": {"type": "string", "enum": ["debito", "credito", "dinheiro"]},
                "parcelas": {"type": "integer"}
            },
            "required": ["valor", "categoria", "descricao"]
        }
    },
    {
        "name": "registrar_receita",
        "description": "Registra uma entrada de dinheiro (receita / income).",
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {"type": "number"},
                "categoria": {"type": "string", "enum": ["Salário", "Investimento", "Presente", "Extra", "Reembolso"]},
                "descricao": {"type": "string"},
                "pagador": {"type": "string"},
                "data": {"type": "string"}
            },
            "required": ["valor", "categoria", "descricao"]
        }
    },
    {
        "name": "resumo_mensal",
        "description": "Retorna um resumo financeiro AGRUPADO (visão geral/totais por categoria). Use para 'resumo', 'saldo do mês' ou 'como foi meu mês'. NÃO use para listar compras individuais.",
        "parameters": {
            "type": "object", 
            "properties": {
                "mes": {"type": "string", "description": "Mês de referência no formato YYYY-MM. Se omitido, usa o atual."},
                "dia_inicio": {"type": "integer", "description": "Dia inicial para o resumo (ex: 1)"},
                "dia_fim": {"type": "integer", "description": "Dia final para o resumo (ex: 4)"}
            }
        }
    },
    {
        "name": "listar_categoria",
        "description": "Lista detalhadamente os gastos de uma categoria específica (ex: Alimentação) em um determinado mês.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "enum": ["Moradia", "Alimentação", "Transporte", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets", "Empresa", "Família e Dependentes"]},
                "mes": {"type": "string", "description": "Mês de referência no formato YYYY-MM."}
            },
            "required": ["categoria"]
        }
    },
    {
        "name": "listar_gastos_detalhados",
        "description": "Lista todas as transações individuais (extrato detalhado/item por item). Use para 'ver gastos', 'extrato', 'lista de compras' ou 'o que eu gastei'.",
        "parameters": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mês no formato YYYY-MM."},
                "dia_inicio": {"type": "integer", "description": "Filtrar a partir deste dia."},
                "dia_fim": {"type": "integer", "description": "Filtrar até este dia."},
                "ordem": {"type": "string", "enum": ["ASC", "DESC"], "description": "Ordem cronológica. Use ASC para ver o início do mês."}
            }
        }
    },
    {
        "name": "consultar_fatura",
        "description": "Consulta os lançamentos e o total especificamente da FATURA DO CARTÃO DE CRÉDITO. Não use para gastos gerais ou débito.",
        "parameters": {
            "type": "object",
            "properties": {"mes": {"type": "string"}}
        }
    },
    {
        "name": "diagnosticar_estouro",
        "description": "Explica o motivo de uma categoria ter estourado o orçamento, listando as subcategorias e despesas mais pesadas.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "enum": ["Moradia", "Alimentação", "Transporte", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets", "Empresa", "Família e Dependentes"]}
            },
            "required": ["categoria"]
        }
    },
    {
        "name": "consultar_limite",
        "description": "Mostra os limites mensais e quanto já foi gasto em cada categoria.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "processar_comando_acerto",
        "description": "Exclui ou altera uma transação específica listada no extrato detalhado anterior. Use quando o usuário pedir para 'apagar o item 1' ou 'mudar categoria do gasto 2'.",
        "parameters": {
            "type": "object",
            "properties": {
                "indice": {"type": "integer", "description": "O número de ordem da transação na lista (ex: 1, 2, 3)"},
                "acao": {"type": "string", "enum": ["excluir", "categoria"], "description": "Ação: 'excluir' para apagar ou 'categoria' para alterar"},
                "valor": {"type": "string", "description": "Obrigatório apenas se acao for 'categoria'. Informe o novo nome da categoria."}
            },
            "required": ["indice", "acao"]
        }
    },
    {
        "name": "definir_limite",
        "description": "Define ou atualiza o limite de gastos para uma categoria específica em um determinado mês.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "enum": ["Moradia", "Alimentação", "Transporte", "Saúde", "Lazer", "Vestuário e Beleza", "Educação", "Financeiro", "Pets", "Empresa", "Família e Dependentes", "Outros"]},
                "valor": {"type": "number", "description": "O valor do limite para a categoria."},
                "mes": {"type": "string", "description": "Mês de referência no formato YYYY-MM. Se omitido, usa o mês atual."}
            },
            "required": ["categoria", "valor"]
        }
    },
    {
        "name": "tendencia_semanal",
        "description": "Retorna o comparativo de gastos diários dos últimos 7 dias.",
        "parameters": {"type": "object", "properties": {}}
    }
]

async def execute(name: str, args: dict, user_phone: str) -> dict[str, Any]:
    logger.info(f"Executando handler nativo: {name} com args {args}")

    def _clean_int(val: Any, default: int) -> int:
        """Extrai apenas os dígitos de uma string para garantir que '9 dias' vire 9."""
        if val is None: return default
        try:
            digits = re.sub(r"\D", "", str(val))
            return int(digits) if digits else default
        except: return default

    def _parse_month_year(raw: Any) -> tuple[int, int] | None:
        """Extrai (ano, mês) de diversos formatos de entrada (IA ou Usuário)."""
        if not raw: return None
        raw_str = str(raw).strip().lower()
        
        # 1. Mapeamento de nomes de meses (PT/EN)
        meses_map = {
            "jan": 1, "january": 1, "janeiro": 1, "feb": 2, "february": 2, "fevereiro": 2,
            "mar": 3, "march": 3, "marco": 3, "apr": 4, "april": 4, "abril": 4,
            "may": 5, "maio": 5, "jun": 6, "june": 6, "junho": 6, "jul": 7, "july": 7, "julho": 7,
            "aug": 8, "august": 8, "agosto": 8, "sep": 9, "september": 9, "setembro": 9,
            "oct": 10, "october": 10, "outubro": 10, "nov": 11, "november": 11, "novembro": 11,
            "dec": 12, "december": 12, "dezembro": 12
        }
        if raw_str in meses_map:
            return date.today().year, meses_map[raw_str]
            
        # 2. Número isolado (ex: "5" ou "05")
        if raw_str.isdigit():
            m = int(raw_str)
            if 1 <= m <= 12: return date.today().year, m

        # 3. Formatos compostos (YYYY-MM, DD/MM/YYYY, MM/YY, etc)
        p = re.split(r"[-/.]", raw_str)
        
        # Caso YYYY-MM-DD (ISO)
        if len(p) == 3 and len(p[0]) == 4: return int(p[0]), int(p[1])
        
        # Caso MM/YYYY ou YYYY-MM
        if len(p) != 2: return None
        
        try:
            v1, v2 = int(p[0]), int(p[1])
            if v1 > 1000: return v1, v2 # YYYY, MM
            if v2 > 1000: return v2, v1 # MM, YYYY
            if v2 < 100: return 2000 + v2, v1 # MM, YY
        except: pass
        return None

    match name:
        case "listar_gastos_detalhados":
            mes_arg = args.get("mes")
            pagina = int(args.get("pagina", 1))
            ordem = args.get("ordem", "DESC")
            dia_inicio = _clean_int(args.get("dia_inicio"), 1)
            dia_fim = _clean_int(args.get("dia_fim"), 0) or None
            categoria = args.get("categoria")
            
            parsed = _parse_month_year(mes_arg) if mes_arg else (date.today().year, date.today().month)
            if not parsed:
                return {"erro": "Não consegui entender o mês solicitado. Use o formato MM/AAAA."}
                
            y, m = parsed
            res_auditoria = listar_transacoes_auditoria(user_phone, m, y, pagina=pagina, ordem=ordem, dia_inicio=dia_inicio, dia_fim=dia_fim, categoria=categoria)
            return {**res_auditoria, "mes_referencia": f"{y}-{m:02d}", "periodo_filtro": {"inicio": dia_inicio, "fim": dia_fim}}

        case "processar_comando_acerto":
            indice = int(args.get("indice", 0))
            acao = args.get("acao")
            valor = args.get("valor")
            return await processar_comando_acerto(user_phone, indice, acao, valor)

        case "definir_limite":
            categoria = args.get("categoria")
            valor = float(args.get("valor", 0))
            mes_ref = args.get("mes") or date.today().strftime("%Y-%m")

            if valor <= 0:
                return {"erro": "O valor do limite deve ser positivo."}

            db.save_budget(user_phone, categoria, valor, mes_ref)
            return {"mensagem": f"✅ Limite de R$ {_fmt_moeda(valor)} para {categoria} em {mes_ref} definido com sucesso!"}

        case "registrar_receita":
            valor = float(args.get("valor") or args.get("amount") or 0)
            categoria = args.get("categoria") or args.get("category")
            descricao = args["descricao"]
            pagador = args.get("pagador")
            data_raw = args.get("data")

            expense_date = date.today()
            if data_raw:
                try: expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date()
                except: pass

            db.save_expense(user_phone, valor, category=categoria, description=descricao, beneficiario=pagador, expense_date=expense_date, transaction_type="income", payment_method="dinheiro")
            return {"registrado": True, "valor": valor, "descricao": descricao, "total_receitas_mes": db.monthly_income_total(user_phone), "tipo": "receita"}

        case "registrar_gasto":
            valor = float(args.get("valor", 0))
            categoria = args.get("categoria") or args.get("category")
            descricao = args.get("descricao") or args.get("description")
            beneficiario = args.get("beneficiario")
            subcategoria = args.get("subcategoria") or args.get("subcategory")
            method = args.get("payment_method") or args.get("metodo_pagamento") or "debito"
            n_parcelas = int(args.get("parcelas") or 1)
            data_raw = args.get("data")

            if not categoria or not descricao:
                return {"erro": "Categoria e descrição são campos obrigatórios."}

            merchant_para_aprender = beneficiario if beneficiario else descricao
            if subcategoria and subcategoria.lower() != "outros" and merchant_para_aprender:
                db.save_user_merchant_mapping(user_phone, merchant_para_aprender, category=categoria, subcategory=subcategoria)

            expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date() if data_raw else date.today()
            if valor <= 0: return {"erro": "Valor inválido."}

            if method == "credito":
                dia_corte, dia_vencimento = db.get_card_settings(user_phone)
                _credito_date = lambda due: date(due.year, due.month, 1)

                if n_parcelas > 1:
                    plano = parcelas(expense_date, valor, n_parcelas, dia_corte, dia_vencimento)
                    for p in plano:
                        due = datetime.strptime(p["fatura_vencimento"], "%Y-%m-%d").date()
                        db.save_expense_credit(user_phone, p["valor"], categoria, descricao, beneficiario, subcategoria, _credito_date(due), p["parcela"], p["total_parcelas"], purchase_date=expense_date)

                    # Calcula o total da categoria para cada mês afetado pelas parcelas
                    detalhes = []
                    for p in plano:
                        mes_ref = p["fatura_vencimento"][:7]
                        detalhes.append({
                            "fatura_label": p["fatura_label"],
                            "valor_parcela": p["valor"],
                            "total_categoria_fatura": db.category_total(user_phone, categoria, mes_ref)
                        })

                    return {
                        "registrado": True,
                        "tipo": "parcelado",
                        "descricao": descricao,
                        "valor": valor,
                        "categoria": categoria,
                        "detalhes": detalhes
                    }
                else:
                    due = fatura_vencimento(expense_date, dia_corte, dia_vencimento)
                    db.save_expense_credit(user_phone, valor, category=categoria, description=descricao, beneficiario=beneficiario, subcategoria=subcategoria, fatura_date=_credito_date(due), purchase_date=expense_date)
                    return {
                        "registrado": True, 
                        "tipo": "credito", 
                        "valor": valor, 
                        "categoria": categoria, 
                        "subcategoria": subcategoria, 
                        "descricao": descricao, 
                        "beneficiario": beneficiario, 
                        "data": expense_date.isoformat(), 
                        "fatura_vencimento": due.isoformat(), 
                        "fatura_label": fatura_label(due), 
                        "total_fatura": db.fatura_total(user_phone, due.isoformat(), dia_corte),
                        "total_categoria_mes": db.category_total(user_phone, categoria, due.strftime("%Y-%m"))
                    }
            else:
                row = db.save_expense(user_phone, valor, categoria, description=descricao, beneficiario=beneficiario, subcategoria=subcategoria, expense_date=expense_date, transaction_type="expense", payment_method=method)
                return {"registrado": True, "id": row.get("id") if isinstance(row, dict) else None, "valor": valor, "categoria": categoria, "subcategoria": subcategoria, "descricao": descricao, "beneficiario": beneficiario, "data": expense_date.isoformat(), "total_categoria_mes": db.category_total(user_phone, category=categoria), "total_mes": db.monthly_total(user_phone)}

        case "consultar_limite":
            mes = date.today().strftime("%Y-%m")
            categoria = args.get("categoria")
            if categoria:
                limite = db.get_budget(user_phone, categoria, mes)
                if not limite: return {"erro": f"Nenhum limite definido para {categoria}."}
                gasto_atual = db.category_total(user_phone, categoria)
                pct = round((gasto_atual / limite) * 100) if limite > 0 else 0
                return {"categoria": categoria, "limite": limite, "gasto_atual": gasto_atual, "percentual_usado": pct}
            limits = db.get_all_budgets(user_phone, mes)
            totais_mes = {c["category"]: float(c["total"]) for c in db.monthly_by_category(user_phone)}
            # Normaliza chaves de gastos para bater com categorias do limite
            totais_norm = {_normalize(k): v for k, v in totais_mes.items()}
            
            processed = []
            for l in limits:
                gasto = totais_norm.get(_normalize(l["category"]), 0.0)
                pct = round((gasto / float(l["amount"])) * 100) if float(l["amount"]) > 0 else 0
                processed.append({
                    "categoria": l["category"],
                    "limite": float(l["amount"]),
                    "gasto_atual": gasto,
                    "percentual_usado": pct,
                    "status": "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
                })
            return {"mes": mes, "limites": processed}

        case "listar_categoria":
            cat_target = args.get("categoria", "").strip()

            if not cat_target:
                return {
                    "mensagem": "🤷‍♂️ Não entendi qual categoria você quer listar. Ex: 'gastos alimentação'"
                }

            hoje = date.today()
            parsed = _parse_month_year(args.get("mes"))
            if parsed:
                hoje = date(parsed[0], parsed[1], 1)

            start_date = hoje.replace(day=1).isoformat()
            ultimo_dia = monthrange(hoje.year, hoje.month)[1]
            end_date = hoje.replace(day=ultimo_dia).isoformat()

            try:
                res = db.get_db().table("finbot_expenses") \
                    .select("subcategory, description, amount, purchase_date, billing_date") \
                    .ilike("user_phone", db._q(user_phone)) \
                    .ilike("category", cat_target.strip()) \
                    .or_("transaction_type.is.null,transaction_type.not.ilike.income") \
                    .gte("billing_date", start_date) \
                    .lte("billing_date", end_date) \
                    .order("billing_date", desc=True) \
                    .execute()

                gastos_raw = res.data or []

                if not gastos_raw:
                    return {
                        "mensagem": f"📭 Nenhum gasto registrado em *{cat_target}* este mês!"
                    }

                mapa_emojis = db.get_category_emojis()
                emoji_da_cat = mapa_emojis.get(cat_target, "📊")
                total_da_cat = sum(float(g["amount"]) for g in gastos_raw)

                sub_agrupado = {}

                for item in gastos_raw:
                    sub = item.get("subcategory")
                    desc = (item.get("description") or "").lower()

                    if not sub or sub.lower() in ["outros", "geral"]:
                        if "ifood" in desc or "rappi" in desc or "uber eats" in desc:
                            sub = "Delivery"
                        elif "mercado" in desc or "carrefour" in desc or "pao de acucar" in desc:
                            sub = "Mercado"
                        elif "cafe" in desc or "café" in desc or "starbucks" in desc:
                            sub = "Café"
                        else:
                            sub = "Outros"
                    else:
                        sub = sub.strip().capitalize()
                        if sub == "Cafe": sub = "Café"

                    valor_item = float(item["amount"])
                    sub_agrupado[sub] = sub_agrupado.get(sub, 0.0) + valor_item

                linhas_sub = []
                for sub_nome, sub_valor in sorted(sub_agrupado.items(), key=lambda x: x[1], reverse=True):
                    pct = round((sub_valor / total_da_cat) * 100) if total_da_cat > 0 else 0
                    linhas_sub.append(f" • *{sub_nome}*: R$ {_fmt_moeda(sub_valor)} ({pct}%)")

                maiores = sorted(gastos_raw, key=lambda x: float(x["amount"]), reverse=True)[:3]
                linhas_maiores = []
                for g in maiores:
                    dt_val = g.get("purchase_date") or g.get("billing_date")
                    dt_fmt = f"{dt_val[8:10]}/{dt_val[5:7]}" if dt_val else "??"
                    linhas_maiores.append(f" • {dt_fmt} | {g['description']}: R$ {_fmt_moeda(float(g['amount']))}")

                recentes = gastos_raw[:5]
                linhas_recentes = []
                for g in recentes:
                    dt_val = g.get("purchase_date") or g.get("billing_date")
                    dt_fmt = f"{dt_val[8:10]}/{dt_val[5:7]}" if dt_val else "??"
                    sub_txt = f" [*{g['subcategory']}*]" if g.get("subcategory") else ""
                    linhas_recentes.append(f" • {dt_fmt} | {g['description']}{sub_txt}: R$ {_fmt_moeda(float(g['amount']))}")

                msg = (
                    f"{emoji_da_cat} *Gastos em {cat_target} ({hoje.strftime('%m/%Y')})*\n\n"
                    f"💸 *Total gasto:* R$ {_fmt_moeda(total_da_cat)}\n\n"
                    f"🔍 *Divisão por Subcategorias:*\n" + "\n".join(linhas_sub)
                )

                if linhas_maiores:
                    msg += "\n\n🔝 *Maiores gastos:*\n" + "\n".join(linhas_maiores)

                if linhas_recentes:
                    msg += "\n\n🧾 *Últimos lançamentos:*\n" + "\n".join(linhas_recentes)

                return {
                    "mensagem": msg,
                    "total_da_cat": total_da_cat,
                    "mes": hoje.strftime("%Y-%m"),
                    "transacoes": gastos_raw
                }

            except Exception as e:
                logger.error(f"Erro ao listar categoria: {e}")
                return {
                    "mensagem": "⚠️ Tive um problema ao buscar os gastos dessa categoria."
                }

        case "resumo_mensal" | "consultar_gastos_do_mes":   
            hoje = date.today()
            parsed = _parse_month_year(args.get("mes"))
            if parsed:
                hoje = date(parsed[0], parsed[1], 1)
            
            mes_referencia = hoje.strftime("%Y-%m")
            dia_inicio = _clean_int(args.get("dia_inicio"), 1)
            dia_fim = _clean_int(args.get("dia_fim"), 0) or None
            
            total_gastos = db.monthly_total(user_phone, mes_referencia, dia_inicio=dia_inicio, dia_fim=dia_fim)
            total_receitas = db.monthly_income_total(user_phone, mes_referencia, dia_inicio=dia_inicio, dia_fim=dia_fim)
            mapa_emojis = db.get_category_emojis()

            # 1. Obter orçamentos e gastos efetivos por categoria
            limites_cadastrados = db.get_all_budgets(user_phone, mes_referencia)
            gastos_por_categoria = db.monthly_by_category(user_phone, mes_referencia, dia_inicio=dia_inicio, dia_fim=dia_fim)
            
            # 2. Mapear limites e consolidar todas as categorias envolvidas
            limites_map = {_normalize(b["category"]): float(b["amount"]) for b in limites_cadastrados}
            
            # Agregação unificada para evitar duplicatas por case-sensitivity (ex: Beleza vs beleza)
            mapa_unificado = {} # { "norm_name": {"display": "...", "gasto": 0.0, "limite": 0.0} }

            for g in gastos_por_categoria:
                nome = g["category"]
                norm = _normalize(nome)
                if norm not in mapa_unificado:
                    mapa_unificado[norm] = {"display": nome, "gasto": 0.0, "limite": 0.0}
                mapa_unificado[norm]["gasto"] += g["total"]

            for norm, valor in limites_map.items():
                if norm in mapa_unificado:
                    mapa_unificado[norm]["limite"] = valor
                    # Prioriza o nome vindo da tabela de orçamentos por ser o "oficial" do sistema
                    mapa_unificado[norm]["display"] = next(l["category"] for l in limites_cadastrados if _normalize(l["category"]) == norm)
                else:
                    # Categoria que tem limite mas não teve gastos ainda
                    # Busca o nome de exibição original da lista de limites
                    nome_original = next(l["category"] for l in limites_cadastrados if _normalize(l["category"]) == norm)
                    mapa_unificado[norm] = {"display": nome_original, "gasto": 0.0, "limite": valor}

            # 3. Ordenar por maior gasto real
            linhas_relatorio = []
            for d in sorted(mapa_unificado.values(), key=lambda x: x["gasto"], reverse=True):
                cat_nome = d["display"]
                gasto_real = d["gasto"]
                limite_valor = d["limite"]
                
                percentual_uso = round((gasto_real / limite_valor) * 100) if limite_valor > 0 else 0
                
                # O farol indica o estado completo da categoria (🔴 Estourado, ⚠️ Atenção, ✅ Saudável)
                status_emoji = "🔴" if (limite_valor > 0 and percentual_uso > 100) else "⚠️" if (limite_valor > 0 and percentual_uso >= 80) else "✅"
                emoji_da_cat = mapa_emojis.get(cat_nome, "📊")
                
                # Formatação visual: se não tiver limite, exibe "S/ Limite"
                txt_pct = f"  {percentual_uso}%" if limite_valor > 0 else ""
                txt_budget = f" / R$ {_fmt_inteiro(limite_valor)}" if limite_valor > 0 else " (S/ Limite)"

                linhas_relatorio.append(
                    f"{status_emoji} {emoji_da_cat} {cat_nome}{txt_pct}\n"
                    f"     R$ {_fmt_moeda(gasto_real)}{txt_budget}"
                )

            periodo_label = hoje.strftime('%m/%Y')
            if dia_fim:
                periodo_label = f"{dia_inicio:02d}/{hoje.month:02d} a {dia_fim:02d}/{hoje.month:02d}"
            elif dia_inicio > 1:
                periodo_label = f"A partir de {dia_inicio:02d}/{hoje.month:02d}"

            # Montagem estruturada final com a linha de 12 traços para evitar quebras
            msg_completa = (
                f"📊 *Resumo {periodo_label}*\n\n"
                f"💰 Receitas:    R$ {_fmt_moeda(total_receitas)}\n"
                f"💸 Gastos:      R$ {_fmt_moeda(total_gastos)}\n"
                f"🏦 Saldo:       R$ {_fmt_moeda(round(total_receitas - total_gastos, 2))}\n\n"
                f"────────────\n"
                + "\n\n".join(linhas_relatorio) +
                f"\n────────────"
            )

            return {
                "tipo_resposta_estruturada": "resumo_mensal", 
                "mes": mes_referencia,
                "dia_inicio": dia_inicio,
                "dia_fim": dia_fim,
                "total_gastos": total_gastos, 
                "total_receitas": total_receitas, 
                "saldo": round(total_receitas - total_gastos, 2), 
                "mensagem": msg_completa
            }

        case "consultar_fatura" | "consultar_fatura_atual":
            dia_corte, dia_vencimento = db.get_card_settings(user_phone)
            parsed = _parse_month_year(args.get("mes") or args.get("mes_ano"))
            
            if parsed:
                year, month = parsed
                last_day = monthrange(year, month)[1]
                due = date(year, month, min(dia_vencimento, last_day))
            else:
                # UX: Por padrão, mostra a fatura com o vencimento mais próximo (hoje ou no futuro).
                # O cálculo original fatura_vencimento(hoje) mostra onde caem compras de HOJE (ex: Julho).
                hoje = date.today()
                last_day_now = monthrange(hoje.year, hoje.month)[1]
                due_this_month = date(hoje.year, hoje.month, min(dia_vencimento, last_day_now))
                
                if hoje <= due_this_month:
                    due = due_this_month
                else:
                    # Se o vencimento deste mês já passou, busca o próximo ciclo (ex: Junho)
                    due = fatura_vencimento(hoje.replace(day=1), dia_corte, dia_vencimento)

            gastos = db.expenses_by_fatura(user_phone, due.isoformat(), dia_corte)

            if not gastos:
                return {"mensagem": f"📅 A sua *{fatura_label(due)}* não possui gastos registrados até o momento."}

            linhas = []
            total_fatura = 0.0
            for g in gastos:
                valor = float(g["amount"])
                total_fatura += valor

                # Extrai a data para exibição (DD/MM)
                dt_raw = g.get("purchase_date") or g.get("billing_date")
                dt_display = f"{dt_raw[8:10]}/{dt_raw[5:7]} | " if dt_raw and len(dt_raw) >= 10 else ""
                
                desc_base = g['description']
                parc_info = f"({g['installment_of']}/{g['installment_total']})" if g.get('installment_of') else None
                
                # Adiciona a parcela apenas se ela já não estiver contida na descrição (evita duplicidade)
                desc_final = f"{desc_base} {parc_info}" if parc_info and parc_info not in desc_base else desc_base
                
                linhas.append(f" • {dt_display}{desc_final}: R$ {_fmt_moeda(valor)}")

            msg = f"💳 *{fatura_label(due).capitalize()}*\n"
            msg += f"📅 Vencimento: {due.strftime('%d/%m/%Y')}\n"
            msg += f"💰 Total: *R$ {_fmt_moeda(total_fatura)}*\n\n"
            msg += "🧾 *Lançamentos:*\n" + "\n".join(linhas)

            return {"tipo_resposta_estruturada": "consultar_fatura", "mensagem": msg, "total": round(total_fatura, 2)}

        case "maiores_gastos":
            gastos = db.get_top_maiores_gastos(user_phone)
            if not gastos: return {"mensagem": "📭 Nenhum gasto registrado ainda este mês."}
            linhas = [f" • {g['description']}: R$ {_fmt_moeda(float(g['amount']))} ({g['category']})" for g in gastos]
            return {"mensagem": "🔝 *Seus maiores gastos do mês:*\n\n" + "\n".join(linhas)}

        case "tendencia_semanal":
            trend = db.get_daily_trend(user_phone)
            if not trend:
                return {"mensagem": "📅 Não encontrei gastos registrados nos últimos 7 dias."}
            
            linhas = []
            total_semana = 0.0
            for t in trend:
                valor = float(t["total"])
                total_semana += valor
                linhas.append(f" • {t['day']}: R$ {_fmt_moeda(valor)}")
            
            msg = "📈 *Evolução dos gastos (Últimos 7 dias):*\n\n" + "\n".join(linhas)
            msg += f"\n\n💰 *Total no período:* R$ {_fmt_moeda(total_semana)}"
            return {"mensagem": msg}

        case "mes_passado":
            resumo = db.get_resumo_mes_anterior(user_phone)
            return {
                "mensagem": f"📅 *Fechamento do Mês Passado*\n\n💰 Receitas: R$ {_fmt_moeda(resumo['receitas'])}\n💸 Gastos Totais: R$ {_fmt_moeda(resumo['gastos'])}\n📉 Saldo: R$ {_fmt_moeda(resumo['saldo'])}"
            }

        case "consultar_saldo":
            saldo = db.monthly_income_total(user_phone) - db.monthly_total(user_phone)
            status_emoji = "🟢" if saldo >= 0 else "🔴"
            return {"mensagem": f"{status_emoji} *Seu Saldo Atual:* R$ {_fmt_moeda(saldo)}\n_(Total de Entradas menos despesas em conta corrente)_"}

        case "listar_parcelamentos":
            compras = db.get_active_installments(user_phone)
            if not compras: return {"mensagem": "💳 Você não possui compras parceladas activas atualmente."}
            linhas = [f" • {c['description']}: R$ {_fmt_moeda(float(c['amount']))} ({c.get('installment_of', 1)}/{c.get('installment_total', 1)}x)" for c in compras]
            return {"mensagem": "💳 *Suas Compras Parceladas Ativas:*\n\n" + "\n".join(linhas)}

        case "diagnosticar_estouro":
            cat_target = args.get("categoria", "").strip()
            if not cat_target: return {"erro": "Categoria necessária para diagnóstico."}
            
            hoje = date.today()
            start_date = hoje.replace(day=1).isoformat()
            ultimo_dia = monthrange(hoje.year, hoje.month)[1]
            end_date = hoje.replace(day=ultimo_dia).isoformat()
            
            limite = None
            target_norm = _normalize(cat_target)
            formatos_timeline = [hoje.strftime("%Y-%m"), hoje.strftime("%m/%Y")]

            for mes_ref_check in formatos_timeline:
                try:
                    todos_limites = db.get_all_budgets(user_phone, mes_ref_check)
                    for b in todos_limites:
                        cat_b_norm = _normalize(b.get("category", ""))
                        if target_norm == cat_b_norm or target_norm in cat_b_norm or cat_b_norm in target_norm:
                            limite = float(b["amount"])
                            cat_target = b["category"]
                            break
                    if limite is not None: break
                except Exception as err:
                    logger.error(f"Erro ao buscar lote no formato {mes_ref_check}: {err}")

            if limite is None:
                for mes_ref_check in formatos_timeline:
                    limite = db.get_budget(user_phone, cat_target, mes_ref_check)
                    if limite is not None: break

            limite = limite or 0.0
            total_gasto = db.category_total(user_phone, category=cat_target)
            
            if total_gasto == 0:
                nome_alternativo = "Alimentacao" if "Alimentação" in cat_target else "Alimentação" if "Alimentacao" in cat_target else cat_target
                total_gasto = db.category_total(user_phone, category=nome_alternativo)
                if total_gasto > 0: cat_target = nome_alternativo

            if total_gasto == 0:
                return {"status": "sem_gastos", "categoria": cat_target}

            try:
                res = db.get_db().table("finbot_expenses")\
                    .select("subcategory, description, amount")\
                    .ilike("user_phone", db._q(user_phone))\
                    .ilike("category", cat_target.strip())\
                    .or_("transaction_type.is.null,transaction_type.not.ilike.income")\
                    .gte("billing_date", start_date)\
                    .lte("billing_date", end_date).execute()

                sub_agrupado = {}
                for item in (res.data or []):
                    s_name = item.get("subcategory")
                    desc_item = item.get("description", "").lower()
                    
                    if not s_name or s_name in ["Outros", "Geral", "outros"]:
                        if "ifood" in desc_item or "rappi" in desc_item or "delivery" in desc_item:
                            s_name = "Delivery"
                        elif "mercado" in desc_item or "carrefour" in desc_item or "pao de acucar" in desc_item:
                            s_name = "Mercado"
                        elif "cafe" in desc_item or "café" in desc_item or "starbucks" in desc_item:
                            s_name = "Café"
                        else:
                            s_name = "Outros"
                            
                    if s_name:
                        s_name = s_name.strip().capitalize()
                        if s_name == "Cafe": s_name = "Café"
                            
                    sub_agrupado[s_name] = sub_agrupado.get(s_name, 0.0) + float(item["amount"])
            except Exception as e:
                logger.error(f"Erro ao agrupar subcategorias: {e}")
                sub_agrupado = {"Outros": total_gasto}

            divisao_subcategorias = {k: round(v, 2) for k, v in sorted(sub_agrupado.items(), key=lambda x: x[1], reverse=True) if v > 0}
            total_gasto_filtrado = sum(divisao_subcategorias.values())

            try:
                res_maiores = db.get_db().table("finbot_expenses")\
                    .select("description, amount")\
                    .ilike("user_phone", db._q(user_phone))\
                    .eq("category", cat_target)\
                    .eq("transaction_type", "expense")\
                    .gte("billing_date", start_date)\
                    .lte("billing_date", end_date)\
                    .order("amount", desc=True).limit(3).execute()
                
                maiores_lancamentos = []
                for g in (res_maiores.data or []):
                    desc_g = g.get("description") or g.get("descricao", "Gasto")
                    maiores_lancamentos.append({"descricao": desc_g, "valor": float(g["amount"])})
            except Exception as e:
                logger.error(f"Erro ao buscar maiores lançamentos: {e}")
                maiores_lancamentos = []

            percentual_total = round((total_gasto_filtrado / limite) * 100) if limite > 0 else 0
            
            mapa_emojis = db.get_category_emojis()
            emoji_da_cat = mapa_emojis.get(cat_target, "📊")

            txt_limite = f"R$ {_fmt_moeda(limite)}" if limite > 0 else "Sem limite fixado"
            pct_str = f"({percentual_total}%)" if limite > 0 else ""
            
            linhas_sub = [f" • *{k}*: R$ {_fmt_moeda(v)} ({round((v/total_gasto_filtrado)*100)}%)" for k, v in divisao_subcategorias.items()]
            linhas_pancadas = [f" • {g['description'] if 'description' in g else g.get('descricao', 'Gasto')}: R$ {_fmt_moeda(g['amount'] if 'amount' in g else g.get('valor', 0))}" for g in maiores_lancamentos]
            
            msg_fallback = f"📊 *Diagnóstico de {emoji_da_cat} {cat_target}*\n\n💸 Total Gasto: *R$ {_fmt_moeda(total_gasto_filtrado)}* {pct_str}\n🎯 Limite do Mês: {txt_limite}\n\n🔍 *Divisão por Subcategorias:*\n" + "\n".join(linhas_sub)
            if linhas_pancadas: 
                msg_fallback += "\n\n🔝 *Os 3 maiores lançamentos isolados:*\n" + "\n".join(linhas_pancadas)

            return {
                "category": cat_target,
                "emoji_categoria": emoji_da_cat,
                "limite": limite,
                "total_gasto": total_gasto_filtrado,
                "percentual_estouro": percentual_total,
                "divisao_subcategorias": divisao_subcategorias,
                "maiores_lancamentos": maiores_lancamentos,
                "mensagem": msg_fallback
            }

        case _:
            return {"mensagem": "Ação executada com sucesso."}


def listar_transacoes_auditoria(user_phone: str, mes: int, ano: int, categoria: str = None, pagina: int = 1, ordem: str = "DESC", dia_inicio: int = 1, dia_fim: int | None = None) -> dict:
    # Normalizamos o filtro de listagem para também respeitar o tipo (evita mostrar receitas no extrato de gastos se desejado, 
    # mas aqui mantemos a listagem ampla e apenas garantimos que os dados brutos cheguem à LLM)
    transacoes_raw = db.obter_transacoes_paginadas(user_phone, mes, ano, categoria=categoria, pagina=pagina, ordem=ordem, dia_inicio=dia_inicio, dia_fim=dia_fim)
    transacoes = transacoes_raw # Mantemos a flexibilidade na listagem
    
    if not transacoes:
        return {"mensagem": "Nenhuma transação encontrada neste período/categoria.", "transacoes": []}

    # Armazena os IDs para que o usuário possa dizer "Acertar 1"
    _SESSAO_LISTAGEM[user_phone] = [t['id'] for t in transacoes]
    
    label_periodo = f"{mes:02d}/{ano}"
    if dia_inicio > 1 or dia_fim:
        label_periodo = f"{dia_inicio or 1:02d} a {dia_fim or 'fim'} de {mes:02d}/{ano}"

    msg = f"📋 *Extrato Detalhado {label_periodo}*\n\n"
    
    for i, tx in enumerate(transacoes, start=1):
        sub = tx.get("subcategory", "Geral") # Pega a subcategoria ou padrão
        cat = tx.get("category", "Sem Categoria")
        valor = tx.get("amount", 0)
        
        # Prioriza a data real da compra (evento) para exibição, caindo no created_at apenas se vazio
        dt_raw = tx.get("purchase_date") or tx.get("created_at")
        dt_display = f"{dt_raw[8:10]}/{dt_raw[5:7]}"

        desc_raw = tx['description']
        parc_info = f"({tx['installment_of']}/{tx['installment_total']})" if tx.get('installment_of') else ""
        
        # Garante que a parcela apareça no extrato mesmo que a descrição seja longa
        clean_desc = (desc_raw[:12] + "..") if len(desc_raw) > 12 else desc_raw
        # Se a parcela já estava no nome (legado), não repetimos
        desc_final = f"{clean_desc} {parc_info}".strip() if parc_info not in desc_raw else clean_desc
    
        # Exibe: Índice | Data | Descrição | *Subcategoria* (Categoria) | Valor
        msg += f"{i}️⃣ {dt_display} | {desc_final} | *{sub}* ({cat}) | R$ {_fmt(valor)}\n"

    msg += f"\n---"
    msg += f"\nPágina {pagina} | Próxima: *Listar {mes} pág {pagina + 1}*"

    msg += f"\n💡 *Para ajustar, digite: Acertar [número] [excluir/subcategoria]*"
    return {"mensagem": msg, "transacoes": transacoes}

async def processar_comando_acerto(user_phone: str, indice: int, acao: str, valor: str = None):
    # Suporte flexível para comandos curtos como 'acertar 4 racao'
    # Se a 'acao' não for um comando reservado e não houver um 'valor' separado,
    # assumimos que a própria 'acao' é o novo nome da subcategoria.
    if acao.lower() not in ["excluir", "categoria", "subcategoria"] and not (valor and valor.strip()):
        valor = acao
        acao = "subcategoria"

    # 1. Recupera o ID da transação da sessão
    sessao = _SESSAO_LISTAGEM.get(user_phone, [])
    
    # Validação do índice (Listas em Python começam em 0, mas exibimos começando em 1)
    if not sessao or indice < 1 or indice > len(sessao):
        return {"mensagem": "⚠️ Não encontrei esse gasto. Liste novamente para atualizar os índices."}
    
    tx_id = sessao[indice - 1]

    # Busca dados atuais para atualizar aprendizado
    res_tx = db.get_db().table("finbot_expenses").select("description, category, subcategory").eq("id", tx_id).execute()
    if not res_tx.data:
         return {"mensagem": "⚠️ Erro ao acessar os dados da transação."}
    
    tx_atual = res_tx.data[0]
    descricao = tx_atual["description"]
    
    # 2. Lógica de Ação
    if acao.lower() == "excluir":
        db.excluir_transacao(tx_id)
        return {"mensagem": "✅ Lançamento excluído com sucesso."}
    
    if acao.lower() in ["categoria", "subcategoria"]:
        coluna = "category" if acao.lower() == "categoria" else "subcategory"
        target_val = valor.strip()
        target_norm = _normalize(target_val)
        
        nova_cat = tx_atual["category"]
        nova_sub = target_val.capitalize() if coluna == "subcategory" else tx_atual["subcategory"]
        if coluna == "category": nova_cat = target_val.capitalize()

        # Se mudou subcategoria, busca a categoria pai correta no banco
        if acao.lower() == "subcategoria":
            try:
                found_in_db = False

                # 1. Tenta encontrar se o valor digitado é na verdade o nome de uma CATEGORIA
                res_all_cats = db.get_db().table("finbot_categories").select("name").execute()
                for c in (res_all_cats.data or []):
                    if _normalize(c["name"]) == target_norm:
                        nova_cat = c["name"]
                        nova_sub = "Geral"
                        found_in_db = True
                        break

                if not found_in_db:
                    # 2. Busca todas as subcategorias para checar nome e keywords
                    res_subs = db.get_db().table("finbot_subcategories").select("category_name, name, keywords").execute()
                    for s in (res_subs.data or []):
                        # Checa se o nome bate ou se o que o usuário digitou está na lista de keywords
                        keywords_norm = [_normalize(kw) for kw in s.get("keywords", [])]
                        if _normalize(s["name"]) == target_norm or target_norm in keywords_norm:
                            nova_cat = s["category_name"]
                            nova_sub = s["name"] # Adota o nome oficial (ex: 'Veterinário')
                            found_in_db = True
                            break
                
                if not found_in_db:
                    # Fallback: Se não achou no banco de subcategorias, usa o categorizador híbrido
                    # Passamos a descrição original + a nova subcategoria para dar mais contexto à IA
                    contexto_busca = f"{descricao} ({nova_sub})"
                    sugestao_cat, sugestao_sub = await categorizar_gasto_hibrido(user_phone, contexto_busca)
                    
                    if sugestao_cat and sugestao_cat != "Perguntar":
                        # Validação: Só aceita a categoria se ela for oficial
                        if any(_normalize(c) == _normalize(sugestao_cat) for c in SISTEMA_CATEGORIAS):
                            nova_cat = next(c for c in SISTEMA_CATEGORIAS if _normalize(c) == _normalize(sugestao_cat))
                        
                        # IMPORTANTE: Mantemos o NOME que o usuário digitou, a menos que a IA 
                        # tenha apenas corrigido a ortografia/acentuação do mesmo termo.
                        if sugestao_sub and _normalize(sugestao_sub) == target_norm:
                            nova_sub = sugestao_sub
            except: pass

        # Prepara pacote de atualização
        updates = {coluna: nova_sub if coluna == "subcategory" else nova_cat}
        if nova_cat != tx_atual["category"]:
            updates["category"] = nova_cat

        # Atualiza TODAS as transações com a mesma descrição para este usuário (Retroatividade)
        try:
            db.get_db().table("finbot_expenses") \
                .update(updates) \
                .eq("user_phone", user_phone) \
                .eq("description", descricao).execute()
        except Exception as e:
            logger.error(f"Erro na atualização retroativa: {e}")
            db.atualizar_transacao(tx_id, updates)

        # Salva o novo mapeamento no banco de dados (Aprendizado futuro)
        db.save_user_merchant_mapping(user_phone, descricao, nova_cat, nova_sub)
        return {"mensagem": f"✅ Ajustado para *{nova_sub}* ({nova_cat}). Apliquei a correção em todos os lançamentos de '{descricao}' e aprendi para os próximos!"}
        
    return {"mensagem": "Comando não reconhecido. Use: 'Acertar [número] [excluir/subcategoria] [valor]'"}