"""
Unit tests for /metrics Prometheus exposition and /metrics/summary endpoint.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from app.gateway import app
from app.metrics import (
    WEBHOOK_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
    LLM_COST_TOTAL,
    get_metrics_summary
)
from app.agent.llm import calculate_cost


@pytest.mark.asyncio
async def test_calculate_cost_accuracy():
    # 1,000,000 prompt tokens @ $0.150 = $0.150
    # 1,000,000 completion tokens @ $0.600 = $0.600
    usd, inr = calculate_cost(1_000_000, 1_000_000)
    assert usd == 0.75
    assert inr == round(0.75 * 85.0, 4)


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Increment a metric
        WEBHOOK_REQUESTS_TOTAL.labels(status="accepted").inc()
        LLM_TOKENS_TOTAL.labels(type="prompt", model="test-model").inc(250)

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "whatsapp_webhook_requests_total" in resp.text
        assert "llm_tokens_total" in resp.text


@pytest.mark.asyncio
async def test_metrics_summary_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "webhook" in data
        assert "lab_api" in data
        assert "bookings" in data
        assert "llm" in data
