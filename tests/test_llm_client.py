"""
Unit tests for LLM abstraction layer (MockLLMClient, OpenAICompatibleLLMClient, cost calculation).
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.config import settings
from app.agent.llm import (
    calculate_cost,
    MockLLMClient,
    OpenAICompatibleLLMClient,
    get_llm_client,
    LLMResponse
)


@pytest.mark.asyncio
async def test_mock_llm_client_routing():
    client = MockLLMClient()
    
    # Check slots query
    resp = await client.chat([{"role": "user", "content": "What slots are available for Lipid Profile tomorrow?"}])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "check_slots"
    assert resp.prompt_tokens > 0
    assert resp.cost_usd > 0
    
    # Booking query
    resp_book = await client.chat([{"role": "user", "content": "Book CBC for 10am"}])
    assert len(resp_book.tool_calls) == 1
    assert resp_book.tool_calls[0]["function"]["name"] == "book_test"
    
    # Escalation query
    resp_help = await client.chat([{"role": "user", "content": "I need help from a human doctor"}])
    assert len(resp_help.tool_calls) == 1
    assert resp_help.tool_calls[0]["function"]["name"] == "handoff_to_human"


def test_calculate_cost():
    usd, inr = calculate_cost(1_000_000, 1_000_000)
    expected_usd = round(settings.COST_PER_1M_PROMPT_TOKENS_USD + settings.COST_PER_1M_COMPLETION_TOKENS_USD, 6)
    assert usd == expected_usd
    assert inr == round(expected_usd * settings.USD_TO_INR_RATE, 4)


def test_get_llm_client_defaults_to_mock():
    with patch.object(settings, "LLM_PROVIDER", "mock"):
        client = get_llm_client()
        assert isinstance(client, MockLLMClient)


def test_get_llm_client_initializes_openai_compatible():
    with patch.object(settings, "LLM_PROVIDER", "deepseek"), \
         patch.object(settings, "LLM_API_KEY", "sk-test-12345"), \
         patch.object(settings, "LLM_BASE_URL", "https://api.deepseek.com"), \
         patch.object(settings, "LLM_MODEL_NAME", "deepseek-chat"):
        client = get_llm_client()
        assert isinstance(client, OpenAICompatibleLLMClient)
        assert client.api_key == "sk-test-12345"
        assert client.base_url == "https://api.deepseek.com"
        assert client.model_name == "deepseek-chat"


@pytest.mark.asyncio
async def test_openai_compatible_client_chat():
    client = OpenAICompatibleLLMClient(
        api_key="sk-test-key",
        base_url="https://api.deepseek.com",
        model_name="deepseek-chat"
    )

    mock_fn = MagicMock()
    mock_fn.name = "check_slots"
    mock_fn.arguments = '{"test_code": "CBC", "date": "2026-11-21"}'

    mock_tc = MagicMock()
    mock_tc.id = "call_123"
    mock_tc.type = "function"
    mock_tc.function = mock_fn

    mock_choice = MagicMock()
    mock_choice.message.content = "Here are the available slots."
    mock_choice.message.tool_calls = [mock_tc]
    mock_usage = MagicMock(prompt_tokens=150, completion_tokens=30)
    mock_response = MagicMock(choices=[mock_choice], usage=mock_usage)

    with patch.object(client.client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_response
        resp = await client.chat([{"role": "user", "content": "Check CBC slots"}])

        assert resp.content == "Here are the available slots."
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "check_slots"
        assert resp.prompt_tokens == 150
        assert resp.completion_tokens == 30
        assert resp.cost_usd > 0
        assert resp.cost_inr > 0
