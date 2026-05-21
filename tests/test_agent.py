# tests/test_agent.py
import pytest
from unittest.mock import patch, AsyncMock

PHONE = "whatsapp:+5511999999999"


@pytest.fixture(autouse=True)
def mock_db():
    with patch("agent.db") as m:
        m.get_history.return_value = []
        m.save_message.return_value = None
        yield m


@pytest.mark.asyncio
async def test_direct_text_response():
    """LLM responde diretamente sem chamar ferramenta."""
    with patch("agent.call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {"type": "text", "content": "Olá! Como posso ajudar?", "provider": "gemini"}

        from app.agent import run
        reply = await run(PHONE, "oi")

        assert reply == "Olá! Como posso ajudar?"
        assert mock_llm.call_count == 1


@pytest.mark.asyncio
async def test_tool_call_flow():
    """LLM chama ferramenta → executa → LLM formata resposta."""
    with (
        patch("agent.call_llm", new_callable=AsyncMock) as mock_llm,
        patch("agent.tool_registry.execute", new_callable=AsyncMock) as mock_tool,
    ):
        mock_llm.side_effect = [
            # Primeira chamada: LLM quer chamar uma ferramenta
            {
                "type": "tool_call",
                "tool_calls": [{"name": "registrar_gasto", "args": {"valor": 35, "categoria": "Alimentação", "descricao": "almoço"}}],
                "provider": "gemini",
            },
            # Segunda chamada: LLM formata a resposta final
            {"type": "text", "content": "✅ R$ 35,00 registrado em Alimentação!", "provider": "gemini"},
        ]
        mock_tool.return_value = {"registrado": True, "total_mes": 35.0}

        from app.agent import run
        reply = await run(PHONE, "almoço 35")

        assert "35" in reply
        assert mock_llm.call_count == 2
        mock_tool.assert_called_once_with("registrar_gasto", {"valor": 35, "categoria": "Alimentação", "descricao": "almoço"}, PHONE)


@pytest.mark.asyncio
async def test_error_handling():
    """Retorna mensagem amigável quando LLM falha."""
    with patch("agent.call_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = RuntimeError("API timeout")

        from app.agent import run
        reply = await run(PHONE, "resumo")

        assert "⚠️" in reply
