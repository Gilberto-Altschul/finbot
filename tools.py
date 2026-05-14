# tools.py
# ─────────────────────────────────────────────────────────────────────────────
# Every capability the agent has lives here.
#
# Each tool has:
#   schema   → sent to the LLM (OpenAI-compatible function calling format)
#   handler  → the actual Python code that runs when the LLM calls the tool
#
# To add a new capability: add to SCHEMAS + add a branch in execute().
# The LLM will start using it automatically.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import re
import logging
from calendar import monthrange
from datetime import date, datetime  # ← datetime importado corretamente aqui
from typing import Any

import database as db
from billing import fatura_vencimento, fatura_label, parcelas

logger = logging.getLogger(__name__)

# ── Tool schemas ──────────────────────────────────────────────────────────────

SCHEMAS: list[dict] = [

    {
        "name": "sincronizar_banco",
        "description": (
            "Busca transações automáticas via Open Finance (Pluggy). "
            "Use quando o usuário perguntar 'o que tem de novo', 'sincronizar banco', "
            "ou quando quiser verificar se houve gastos não registrados manualmente."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "arquivo": {
                    "type": "string",
                    "description": "Caminho do arquivo JSON para importar (opcional, apenas para testes)."
                },
                "account_id": {
                    "type": "string",
                    "description": "ID de uma conta específica (opcional). Se omitido, sincroniza o banco inteiro."
                }
            }
        }
    },
    {
        "name": "registrar_gasto",
        "description": (
            "Registra um gasto do usuário. "
            "Use sempre que o usuário mencionar uma despesa, compra, gasto ou pagamento. "
            "Infira a categoria a partir da descrição quando não for explícita."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {
                    "type": "number",
                    "description": "Valor total da compra em reais. Converta vírgula para ponto (ex: '12,50' → 12.5). Em compras parceladas, é o valor total e não da parcela.",
                },
                "categoria": {
                    "type": "string",
                    "enum": [
                        "Alimentação", "Transporte", "Moradia",
                        "Saúde", "Lazer", "Pessoal", "Educação", "Financeiro", "Pets",
                    ],
                    "description": (
                        "Categoria mais adequada. Guia: "
                        "Alimentação=mercado/delivery/padaria/almoço-diário; "
                        "Transporte=uber/combustível/metrô/manutenção; "
                        "Moradia=aluguel/luz/internet/condomínio/faxina; "
                        "Saúde=farmácia/médico/academia/plano; "
                        "Lazer=restaurante/bar/streaming/cinema/viagem/balada; "
                        "Pessoal=roupa/cabelo/beleza/presente; "
                        "Pets=pet/ração/veterinário/banho; "
                        "Educação=curso/livro/escola/faculdade/linkedin; "
                        "Financeiro=parcela/seguro/empréstimo/investimento"
                    ),
                },
                "subcategoria": {
                    "type": "string",
                    "description": (
                        "Subcategoria inferida pelo contexto. Exemplos: "
                        "Delivery, Mercado, Refeição, Restaurante, Combustível, Aplicativo, "
                        "Streaming, Academia, Farmácia, Financiamento, Presente, Pet"
                    ),
                },
                "descricao": {
                    "type": "string",
                    "description": "Descrição curta do gasto (ex: 'almoço', 'uber', 'conta de luz')",
                },
                "beneficiario": {
                    "type": "string",
                    "description": "Quem recebeu o pagamento ou estabelecimento favorecido (ex: 'João', 'iFood', 'Farmácia São Paulo')",
                },
                "data": {
                    "type": "string",
                    "description": "Data do gasto no formato YYYY-MM-DD. Usar apenas quando o usuário informar uma data diferente de hoje.",
                },
                "payment_method": {
                    "type": "string",
                    "enum": ["debito", "credito", "dinheiro"],
                    "description": "Meio de pagamento. Padrão: debito. Use 'credito' se mencionar cartão.",
                },
                "parcelas": {
                    "type": "integer",
                    "description": "Número de parcelas. Só para crédito. Omitir se não for parcelado.",
                },
            },
            "required": ["valor", "categoria", "descricao"],
        },
    },
    {
        "name": "registrar_receita",
        "description": "Registra uma entrada de dinheiro (salário, pix recebido, venda, etc).",
        "parameters": {
            "type": "object",
            "properties": {
                "valor": {"type": "number", "description": "Valor recebido."},
                "categoria": {
                    "type": "string",
                    "enum": ["Salário", "Investimento", "Presente", "Extra", "Reembolso"],
                },
                "descricao": {"type": "string", "description": "Ex: 'Salário Mensal', 'Venda OLX'"},
                "pagador": {"type": "string", "description": "Quem enviou o dinheiro (opcional)."},
                "data": {"type": "string", "description": "Formato YYYY-MM-DD (opcional)."},
            },
            "required": ["valor", "categoria", "descricao"],
        },
    },
    {
        "name": "resumo_mensal",
        "description": "Retorna o resumo financeiro do mês atual: gastos por categoria, total de gastos, total de receitas e saldo.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ultimos_gastos",
        "description": "Retorna os últimos gastos registrados pelo usuário.",
        "parameters": {
            "type": "object",
            "properties": {
                "quantidade": {"type": "integer", "description": "Número de gastos a retornar (máx 10). Padrão: 5."}
            },
            "required": [],
        },
    },
    {
        "name": "tendencia_semanal",
        "description": "Retorna os gastos diários dos últimos 7 dias.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "consultar_fatura",
        "description": "Consulta os gastos na fatura do cartão de crédito.",
        "parameters": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Mês no formato YYYY-MM para consultar fatura específica. Omitir para fatura atual."}
            },
            "required": [],
        },
    },
    {
        "name": "listar_categoria",
        "description": "Lista todos os gastos de uma categoria no mes atual com data, descricao, beneficiario e subcategoria.",
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "enum": [
                        "Alimentação", "Transporte", "Moradia",
                        "Saúde", "Lazer", "Pessoal", "Educação", "Financeiro", "Pets",
                    ],
                    "description": "Categoria a listar.",
                },
                "limite": {
                    "type": "integer",
                    "description": "Numero maximo de itens. Padrao: 50.",
                },
            },
            "required": ["categoria"],
        },
    },
    {
        "name": "configurar_cartao",
        "description": "Configura o dia de vencimento da fatura do cartão do usuário.",
        "parameters": {
            "type": "object",
            "properties": {
                "dia_vencimento": {"type": "integer", "description": "Dia do mês em que a fatura vence (1-28)."}
            },
            "required": ["dia_vencimento"],
        },
    },
    {
        "name": "definir_limite",
        "description": (
            "Define ou atualiza o limite de orçamento mensal para uma categoria. "
            "Use quando o usuário disser: 'limite alimentação 2000', 'meta transporte 500', "
            "'teto moradia 3000', 'orçamento saúde 400'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "enum": [
                        "Alimentação", "Transporte", "Moradia",
                        "Saúde", "Lazer", "Pessoal", "Educação", "Financeiro",
                    ],
                    "description": "Categoria do limite",
                },
                "valor": {
                    "type": "number",
                    "description": "Valor do limite mensal em reais",
                },
                "mes": {
                    "type": "string",
                    "description": "Mês de referência no formato YYYY-MM. Se omitido, usa o mês atual.",
                },
            },
            "required": ["categoria", "valor"],
        },
    },
    {
        "name": "consultar_limite",
        "description": (
            "Consulta o limite definido para uma categoria ou mostra todos os limites. "
            "Use quando o usuário perguntar: 'qual meu limite de alimentação?', "
            "'histórico do limite de transporte', 'meus limites'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "description": "Categoria específica. Se omitido, retorna todos os limites do mês.",
                },
                "historico": {
                    "type": "boolean",
                    "description": "Se true, retorna o histórico de alterações do limite.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "listar_categorias_disponiveis",
        "description": "Lista todas as categorias e subcategorias de gastos e receitas que o FinBot reconhece.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

# ── Tool handlers ─────────────────────────────────────────────────────────────

async def execute(name: str, args: dict, user_phone: str) -> dict[str, Any]:
    logger.info("Tool called", extra={"tool": name, "tool_args": args})

    match name:

        case "registrar_receita":
            valor = args["valor"]
            categoria = args["categoria"]
            descricao = args["descricao"]
            pagador = args.get("pagador")
            data_raw = args.get("data")

            expense_date = date.today()
            if data_raw:
                expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date()

            row = db.save_expense(
                user_phone, valor, categoria, descricao,
                beneficiario=pagador, expense_date=expense_date, transaction_type="income"
            )
            return {
                "registrado": True,
                "valor": valor,
                "descricao": descricao,
                "total_receitas_mes": db.monthly_income_total(user_phone),
                "tipo": "receita"
            }

        case "registrar_gasto":
            valor: float = args.get("valor", 0)
            categoria: str = args["categoria"]
            descricao: str = args["descricao"]
            beneficiario: str | None = args.get("beneficiario")
            subcategoria: str | None = args.get("subcategoria")
            method: str = args.get("payment_method", "debito")
            n_parcelas: int = int(args.get("parcelas") or 1)

            data_raw = args.get("data")
            if data_raw:
                expense_date = datetime.strptime(data_raw, "%Y-%m-%d").date()
            else:
                expense_date = date.today()

            if valor <= 0:
                return {"erro": "Valor inválido. Informe um valor positivo."}

            if method == "credito":
                dia_corte, dia_vencimento = db.get_card_settings(user_phone)

                def _credito_date(due: date) -> date:
                    # Salva no 1o dia do mes do vencimento da fatura.
                    # O resumo e a consulta de fatura filtram por mes do created_at.
                    return date(due.year, due.month, 1)

                if n_parcelas > 1:
                    plano = parcelas(expense_date, valor, n_parcelas, dia_corte, dia_vencimento)
                    for p in plano:
                        due = datetime.strptime(p["fatura_vencimento"], "%Y-%m-%d").date()
                        parcela_date = _credito_date(due)

                        row = db.save_expense_credit(
                            user_phone=user_phone,
                            amount=p["valor"],
                            category=categoria,
                            description=f"{descricao} ({p['parcela']}/{p['total_parcelas']})",
                            beneficiario=beneficiario,
                            subcategoria=subcategoria,
                            installment_of=p["parcela"],
                            installment_total=p["total_parcelas"],
                            expense_date=parcela_date,
                        )
                        if not row or not row.get("id"):
                            return {"erro": "Falha ao gravar parcela no banco."}

                    return {
                        "registrado": True,
                        "tipo": "parcelado",
                        "descricao": descricao,
                        "beneficiario": beneficiario,
                        "valor_total": valor,
                        "data": expense_date.isoformat(),
                        "parcelas": plano,
                    }
                else:
                    due = fatura_vencimento(expense_date, dia_corte, dia_vencimento)
                    credito_date = _credito_date(due)
                    row = db.save_expense_credit(
                        user_phone=user_phone,
                        amount=valor,
                        category=categoria,
                        description=descricao,
                        beneficiario=beneficiario,
                        subcategoria=subcategoria,
                        expense_date=credito_date,
                    )
                    if not row or not row.get("id"):
                        return {"erro": "Falha ao gravar no banco (Crédito)."}

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
                    }

            row = db.save_expense(
                user_phone,
                valor,
                categoria,
                descricao,
                beneficiario=beneficiario,
                subcategoria=subcategoria,
                expense_date=expense_date,
            )
            logger.info(f"db.save_expense returned: {row}")
            if not row.get("id"):
                return {"erro": "Falha ao gravar no banco. Verifique se a coluna 'beneficiario' existe na tabela 'finbot_expenses'."}

            total_categoria = db.category_total(user_phone, categoria)
            total_mes = db.monthly_total(user_phone)

            return {
                "registrado": True,
                "id": row.get("id"),
                "valor": valor,
                "categoria": categoria,
                "subcategoria": subcategoria,
                "descricao": descricao,
                "beneficiario": beneficiario,
                "data": expense_date.isoformat(),
                "total_categoria_mes": total_categoria,
                "total_mes": total_mes,
            }

        case "resumo_mensal":
            mes = date.today().strftime("%Y-%m")
            por_categoria = db.monthly_by_category(user_phone)
            total_gastos = db.monthly_total(user_phone)
            total_receitas = db.monthly_income_total(user_phone)

            # Enrich with budget data
            limites = {b["category"]: float(b["amount"]) for b in db.get_all_budgets(user_phone, mes)}
            categorias_com_limite = []
            for cat in por_categoria:
                limite = limites.get(cat["category"])
                percentual = round(float(cat["total"]) / limite * 100) if limite else None
                categorias_com_limite.append({
                    **cat,
                    "limite": limite,
                    "percentual_usado": percentual,
                    "status": "🔴" if percentual and percentual > 100 else "⚠️" if percentual and percentual >= 80 else "✅" if percentual else None,
                })

            return {
                "por_categoria": categorias_com_limite,
                "total_gastos": total_gastos,
                "total_receitas": total_receitas,
                "saldo": round(total_receitas - total_gastos, 2),
            }

        case "total_categoria":
            categoria = args["categoria"]
            total = db.category_total(user_phone, categoria)
            return {"categoria": categoria, "total": total}

        case "ultimos_gastos":
            quantidade = min(int(args.get("quantidade", 5)), 10)
            gastos = db.recent_expenses(user_phone, quantidade)
            return {"gastos": gastos}

        case "tendencia_semanal":
            dias = db.daily_trend(user_phone, 7)
            total_semana = round(sum(float(d["total"]) for d in dias), 2)
            return {"dias": dias, "total_semana": total_semana}

        case "configurar_cartao":
            dia_vencimento = int(args["dia_vencimento"])
            if not 1 <= dia_vencimento <= 28:
                return {"erro": "Dia de vencimento deve ser entre 1 e 28."}
            dia_corte = dia_vencimento - 7 if dia_vencimento > 7 else dia_vencimento - 7 + 30
            db.save_user_settings(user_phone, dia_vencimento, dia_corte)
            return {
                "configurado": True,
                "dia_vencimento": dia_vencimento,
                "dia_corte": dia_corte,
            }

        case "consultar_fatura":
            dia_corte, dia_vencimento = db.get_card_settings(user_phone)
            mes = args.get("mes")
            if mes:
                from calendar import monthrange as _mr
                year, month = int(mes[:4]), int(mes[5:7])
                last_day = _mr(year, month)[1]
                due = date(year, month, min(dia_vencimento, last_day))
            else:
                due = fatura_vencimento(date.today(), dia_corte, dia_vencimento)
            gastos = db.expenses_by_fatura(user_phone, due.isoformat(), dia_corte)
            total = round(sum(float(g["amount"]) for g in gastos), 2)
            return {
                "fatura": fatura_label(due),
                "vencimento": due.isoformat(),
                "total": total,
                "gastos": gastos,
            }

        case "listar_categoria":
            categoria = args["categoria"]
            limite = min(int(args.get("limite", 50)), 50)
            gastos = db.category_expenses_detail(user_phone, categoria, limite)
            total = round(sum(float(g["amount"]) for g in gastos), 2)
            return {
                "categoria": categoria,
                "gastos": gastos,
                "total": total,
                "count": len(gastos),
            }

        case "definir_limite":
            categoria = args["categoria"]
            valor = float(args["valor"])
            mes = args.get("mes") or date.today().strftime("%Y-%m")

            if valor <= 0:
                return {"erro": "Valor do limite deve ser positivo."}

            db.save_budget(user_phone, categoria, valor, mes)

            # Check current spending vs new limit
            gasto_atual = db.category_total(user_phone, categoria)
            percentual = round(gasto_atual / valor * 100) if valor > 0 else 0

            return {
                "definido": True,
                "categoria": categoria,
                "limite": valor,
                "mes": mes,
                "gasto_atual": gasto_atual,
                "percentual_usado": percentual,
            }

        case "consultar_limite":
            mes = date.today().strftime("%Y-%m")
            categoria = args.get("categoria")
            historico = args.get("historico", False)

            if categoria and historico:
                hist = db.get_budget_history(user_phone, categoria)
                return {"categoria": categoria, "historico": hist}

            if categoria:
                limite = db.get_budget(user_phone, categoria, mes)
                gasto = db.category_total(user_phone, categoria)
                percentual = round(gasto / limite * 100) if limite else None
                return {
                    "categoria": categoria,
                    "limite": limite,
                    "gasto_atual": gasto,
                    "percentual_usado": percentual,
                    "mes": mes,
                }

            # All categories
            limites = db.get_all_budgets(user_phone, mes)
            resultado = []
            for b in limites:
                gasto = db.category_total(user_phone, b["category"])
                percentual = round(gasto / float(b["amount"]) * 100) if b["amount"] else 0
                resultado.append({
                    "categoria": b["category"],
                    "limite": float(b["amount"]),
                    "gasto_atual": gasto,
                    "percentual_usado": percentual,
                    "status": "🔴" if percentual > 100 else "⚠️" if percentual >= 80 else "✅",
                })
            return {"limites": resultado, "mes": mes}

        case "sincronizar_banco":
            try:
                from pluggy_service import PluggyService
                pluggy = PluggyService()

                arquivo = args.get("arquivo")
                account_id = args.get("account_id")

                if arquivo:
                    resultado = pluggy.sync_from_file(user_phone, arquivo)
                else:
                    resultado = pluggy.sync_user_transactions(user_phone, account_id=account_id)

                return {"status": "concluido", "mensagem": resultado}
                
            except Exception as e:
                logger.error(f"Erro na tool sincronizar_banco: {e}")
                return {"error": "Não foi possível sincronizar com o banco agora."}

        case "listar_categorias_disponiveis":
            expense_categories = []
            income_categories = []
            subcategories = set()

            for schema in SCHEMAS:
                if schema["name"] == "registrar_gasto":
                    expense_categories = schema["parameters"]["properties"]["categoria"]["enum"]
                    subcat_desc = schema["parameters"]["properties"]["subcategoria"]["description"]
                    if "Exemplos: " in subcat_desc:
                        subcat_list_str = subcat_desc.split("Exemplos: ", 1)[1]
                        for item in re.split(r",\s*", subcat_list_str):
                            if item.strip(): subcategories.add(item.strip())
                elif schema["name"] == "registrar_receita":
                    income_categories = schema["parameters"]["properties"]["categoria"]["enum"]

            return {
                "expense_categories": sorted(list(set(expense_categories))),
                "income_categories": sorted(list(set(income_categories))),
                "example_subcategories": sorted(list(subcategories)),
            }

        case _:
            raise ValueError(f"Ferramenta desconhecida: {name}")