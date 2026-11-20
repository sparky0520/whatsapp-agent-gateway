"""
Unit tests for atomic message deduplication via Redis SET NX EX.
"""
import pytest
import hmac
import hashlib
import json
from httpx import AsyncClient, ASGITransport
from app.gateway import app
from app.redis_client import redis_client
from app.config import settings


@pytest.mark.asyncio
async def test_redis_deduplication_atomic():
    wamid = "wamid.HBgM1234567890AA=="

    # First check should be fresh
    fresh_1 = await redis_client.deduplicate_message(wamid, ttl_seconds=60)
    assert fresh_1 is True

    # Immediate second check should be detected as duplicate
    fresh_2 = await redis_client.deduplicate_message(wamid, ttl_seconds=60)
    assert fresh_2 is False

    # Another unique wamid should be fresh
    wamid_new = "wamid.HBgM9876543210BB=="
    assert await redis_client.deduplicate_message(wamid_new, ttl_seconds=60) is True


@pytest.mark.asyncio
async def test_gateway_deduplication_e2e():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        wamid = "wamid.HBgMTESTDEDUP001=="
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "ACC1",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "contacts": [{"wa_id": "919999888877", "profile": {"name": "Test Patient"}}],
                                "messages": [
                                    {
                                        "from": "919999888877",
                                        "id": wamid,
                                        "timestamp": "1732085400",
                                        "text": {"body": "Check CBC slots"},
                                        "type": "text"
                                    }
                                ]
                            },
                            "field": "messages"
                        }
                    ]
                }
            ]
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = f"sha256={hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()}"
        headers = {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}

        # First request: should be accepted & queued
        resp1 = await client.post("/webhook", content=body_bytes, headers=headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["enqueued"] == 1
        assert data1["duplicates"] == 0

        # Second request with SAME wamid (simulating Meta duplicate retry):
        resp2 = await client.post("/webhook", content=body_bytes, headers=headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        # Must acknowledge with 200 OK, but mark duplicate and NOT enqueue!
        assert data2["enqueued"] == 0
        assert data2["duplicates"] == 1
