# tests/test_tools.py
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

PHONE = "whatsapp:+5511999999999"

MOCK_EXPENSE_ROW = {"id": 1, "amount": 35.0, "category": "Alimentação", "description": "almoço"}


@pytest.fixture(autouse=True)
def mock_db():
    with patch("tools.db") as m:
        m.save_expense.return_value = MOCK_EXPENSE_ROW
        m.category_total.return_value = 320.5
        m.monthly_total.return_value = 415.5
        m.monthly_by_category.return_value = [
            {"category": "Alimentação", "total": 320.5, "count": 8},
            {"category": "Transporte",  "total": 95.0,  "count": 4},
        ]
        m.recent_expenses.return_value = [MOCK_EXPENSE_ROW]
        m.daily_trend.return_value = [
            {"day": "01/05", "total": 80},
            {"day": "02/05", "total": 45},
        ]
        yield m


@pytest.mark.asyncio
async def test_registrar_gasto():
    from tools import execute
    result = await execute("registrar_gasto", {"valor": 35, "categoria": "Alimentação", "descricao": "almoço"}, PHONE)
    assert result["registrado"] is True
    assert result["valor"] == 35
    assert result["total_mes"] == 415.5


@pytest.mark.asyncio
async def test_registrar_gasto_valor_invalido():
    from tools import execute
    result = await execute("registrar_gasto", {"valor": -5, "categoria": "Alimentação", "descricao": "teste"}, PHONE)
    assert "erro" in result


@pytest.mark.asyncio
async def test_resumo_mensal():
    from tools import execute
    result = await execute("resumo_mensal", {}, PHONE)
    assert result["total"] == 415.5
    assert len(result["por_categoria"]) == 2


@pytest.mark.asyncio
async def test_total_categoria():
    from tools import execute
    result = await execute("total_categoria", {"categoria": "Alimentação"}, PHONE)
    assert result["total"] == 320.5


@pytest.mark.asyncio
async def test_ultimos_gastos():
    from tools import execute
    result = await execute("ultimos_gastos", {"quantidade": 5}, PHONE)
    assert len(result["gastos"]) == 1


@pytest.mark.asyncio
async def test_ultimos_gastos_cap():
    from tools import execute
    import tools
    with patch.object(tools.db, "recent_expenses", return_value=[]) as mock_recent:
        await execute("ultimos_gastos", {"quantidade": 99}, PHONE)
        mock_recent.assert_called_with(PHONE, 10)


@pytest.mark.asyncio
async def test_tendencia_semanal():
    from tools import execute
    result = await execute("tendencia_semanal", {}, PHONE)
    assert result["total_semana"] == 125.0


@pytest.mark.asyncio
async def test_ferramenta_desconhecida():
    from tools import execute
    with pytest.raises(ValueError):
        await execute("ferramenta_inexistente", {}, PHONE)
