# app/tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Recursos Executáveis do FinBot — Versão Unificada e Alinhada com Legado
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import re
import logging
from calendar import monthrange
from datetime import date, datetime
from typing import Any

import app.database as db
from app.billing import fatura_vencimento, fatura_label, parcelas

logger = logging.getLogger(__name__)

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
                "categoria": {"type": "string", "enum": ["Alimentação", "Transporte", "Moradia", "Saúde", "Lazer", "Pessoal", "Educação", "Financeiro", "Pets", "Empresa", "Outros"]},
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
        "description": "Registra uma entrada de dinheiro.",
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
        "description": "Retorna o resumo financeiro do mês atual.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "consultar_fatura",
        "description": "Consulta os gastos na fatura do cartão de crédito.",
        "parameters": {
            "type": "object",
            "properties": {"mes": {"type": "string"}}
        }
    },
    {
        "name": "definir_limite",
        "description": "Define uma meta de gastos (orçamento) para uma categoria.",
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {"type": "number"},
                "categoria": {"type": "string", "enum": ["Alimentação", "Transporte", "Moradia", "Saúde", "Lazer", "Pessoal", "Educação", "Financeiro", "Pets", "Empresa"]},
                "mes": {"type": "string", "description": "Mês no formato YYYY-MM"}
            },
            "required": ["valor", "categoria"]
        }
    },
    {
        "name": "consultar_limite",
        "description": "Consulta o orçamento e quanto já foi gasto.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string"},
                "historico": {"type": "boolean"}
            }
        }
    }
]

# ── Executor de Capabilidades ─────────────────────────────────────────────────

def execute(name: str, args: dict, user_phone: str) -> dict[str, Any]:
    logger.info(f"Executando handler nativo: {name} com args {args}")

    match name:
        case "registrar_receita":
            valor = float(args["valor"])
            categoria = args["categoria"]
            descricao = args["descricao"]
            pagador = args.get("pagador")
            data_raw = args.get("data")

            expense_date = date.today()
            if data_raw:
                try: expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date()
                except: pass

            db.save_expense(user_phone, valor, categoria, descricao, beneficiario=pagador, expense_date=expense_date, transaction_type="income", payment_method="dinheiro")
            return {"registrado": True, "valor": valor, "descricao": descricao, "total_receitas_mes": db.monthly_income_total(user_phone), "tipo": "receita"}

        case "registrar_gasto":
            valor = float(args.get("valor", 0))
            # Suporta tanto 'categoria' quanto 'category' para blindar chamadas antigas da LLM
            categoria = args.get("categoria") or args.get("category")
            descricao = args.get("descricao") or args.get("description")
            beneficiario = args.get("beneficiario")
            subcategoria = args.get("subcategoria")
            method = args.get("payment_method") or args.get("metodo_pagamento") or "debito"
            n_parcelas = int(args.get("parcelas") or 1)
            data_raw = args.get("data")

            if not categoria or not descricao:
                return {"erro": "Categoria e descrição são campos obrigatórios."}

            expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date() if data_raw else date.today()
            if valor < 0: return {"erro": "Valor inválido."}

            if method == "credito":
                dia_corte, dia_vencimento = db.get_card_settings(user_phone)
                _credito_date = lambda due: date(due.year, due.month, 1)

                if n_parcelas > 1:
                    plano = parcelas(expense_date, valor, n_parcelas, dia_corte, dia_vencimento)
                    for p in plano:
                        due = datetime.strptime(p["fatura_vencimento"], "%Y-%m-%d").date()
                        db.save_expense_credit(user_phone, p["valor"], categoria, f"{descricao} ({p['parcela']}/{p['total_parcelas']})", beneficiario, subcategoria, _credito_date(due), p["parcela"], p["total_parcelas"])
                    return {"registrado": True, "tipo": "parcelado", "descricao": descricao, "beneficiario": beneficiario, "valor_total": valor, "data": expense_date.isoformat(), "parcelas": plano}
                else:
                    due = fatura_vencimento(expense_date, dia_corte, dia_vencimento)
                    db.save_expense_credit(user_phone, valor, categoria, descricao, beneficiario, subcategoria, _credito_date(due))
                    return {"registrado": True, "tipo": "credito", "valor": valor, "categoria": categoria, "subcategoria": subcategoria, "descricao": descricao, "beneficiario": beneficiario, "data": expense_date.isoformat(), "fatura_vencimento": due.isoformat(), "fatura_label": fatura_label(due), "total_fatura": db.fatura_total(user_phone, due.isoformat(), dia_corte)}
            else:
                row = db.save_expense(user_phone, valor, categoria, descricao, beneficiario, subcategoria, expense_date, "expense", method)
                return {"registrado": True, "id": row.get("id"), "valor": valor, "categoria": categoria, "subcategoria": subcategoria, "descricao": descricao, "beneficiario": beneficiario, "data": expense_date.isoformat(), "total_categoria_mes": db.category_total(user_phone, categoria), "total_mes": db.monthly_total(user_phone)}

        case "resumo_mensal" | "consultar_gastos_do_mes":
            mes = date.today().strftime("%Y-%m")
            por_categoria = db.monthly_by_category(user_phone)
            total_gastos = db.monthly_total(user_phone)
            total_receitas = db.monthly_income_total(user_phone)

            limites = {b["category"]: float(b["amount"]) for b in db.get_all_budgets(user_phone, mes)}
            categorias_com_limite = []
            for cat in por_categoria:
                limite = limites.get(cat["category"])
                percentual = round(float(cat["total"]) / limite * 100) if limite else None
                categorias_com_limite.append({**cat, "limite": limite, "percentual_usado": percentual, "status": "🔴" if percentual and percentual > 100 else "⚠️" if percentual and percentual >= 80 else "✅" if percentual else None})

            return {"tipo_resposta_estruturada": "resumo_mensal", "por_categoria": sorted(categorias_com_limite, key=lambda x: float(x["total"]), reverse=True), "total_gastos": total_gastos, "total_receitas": total_receitas, "saldo": round(total_receitas - total_gastos, 2)}

        case "consultar_fatura" | "consultar_fatura_atual":
            dia_corte, dia_vencimento = db.get_card_settings(user_phone)
            mes = args.get("mes") or args.get("mes_ano")
            
            if mes and "-" in mes:
                partes = mes.split("-")
                year, month = (int(partes[0]), int(partes[1])) if len(partes[0]) == 4 else (int(partes[1]), int(partes[0]))
                last_day = monthrange(year, month)[1]
                due = date(year, month, min(dia_vencimento, last_day))
            else:
                due = fatura_vencimento(date.today(), dia_corte, dia_vencimento)

            gastos = db.expenses_by_fatura(user_phone, due.isoformat(), dia_corte)
            return {"tipo_resposta_estruturada": "consultar_fatura", "fatura": fatura_label(due), "vencimento": due.isoformat(), "total": round(sum(float(g["amount"]) for g in gastos), 2), "gastos": gastos}

        case "definir_limite":
            valor = float(args["valor"])
            categoria = args["categoria"]
            mes = args.get("mes") or date.today().strftime("%Y-%m")
            db.save_budget(user_phone, categoria, valor, mes)
            gasto_atual = db.category_total(user_phone, categoria)
            pct = round((gasto_atual / valor) * 100) if valor > 0 else 0
            return {"categoria": categoria, "limite": valor, "gasto_atual": gasto_atual, "percentual_usado": pct, "mes": mes}

        case "consultar_limite":
            mes = date.today().strftime("%Y-%m")
            categoria = args.get("categoria")
            if args.get("historico") and categoria:
                return {"categoria": categoria, "historico": db.get_budget_history(user_phone, categoria)}
            if categoria:
                limite = db.get_budget_limit(user_phone, categoria)
                if not limite: return {"erro": f"Nenhum limite definido para {categoria}."}
                gasto_atual = db.category_total(user_phone, categoria)
                pct = round((gasto_atual / limite) * 100) if limite > 0 else 0
                return {"categoria": categoria, "limite": limite, "gasto_atual": gasto_atual, "percentual_usado": pct}
            limits = db.get_all_budgets(user_phone, mes)
            # Otimização: Busca todos os totais do mês uma única vez em vez de N vezes no loop
            totais_mes = {c["category"]: float(c["total"]) for c in db.monthly_by_category(user_phone)}
            processed = []
            for l in limits:
                gasto = totais_mes.get(l["category"], 0.0)
                pct = round((gasto / float(l["amount"])) * 100) if float(l["amount"]) > 0 else 0
                processed.append({
                    "categoria": l["category"],
                    "limite": float(l["amount"]),
                    "gasto_atual": gasto,
                    "percentual_usado": pct,
                    "status": "🔴" if pct > 100 else "⚠️" if pct >= 80 else "✅"
                })
            return {"mes": mes, "limites": processed}

        case _:
            return {"mensagem": "Ação executada."}