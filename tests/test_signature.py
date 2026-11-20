"""
Unit tests for WhatsApp HMAC-SHA256 signature verification and GET challenge.
"""
import pytest
import hmac
import hashlib
import json
from httpx import AsyncClient, ASGITransport
from app.gateway import app, verify_whatsapp_signature
from app.config import settings


@pytest.mark.asyncio
async def test_verify_whatsapp_signature_valid():
    body = b'{"hello": "world"}'
    sig = hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={sig}"

    assert verify_whatsapp_signature(body, header) is True


@pytest.mark.asyncio
async def test_verify_whatsapp_signature_invalid():
    body = b'{"hello": "world"}'
    header = "sha256=invalid_hex_digest_123456"

    assert verify_whatsapp_signature(body, header) is False


@pytest.mark.asyncio
async def test_verify_whatsapp_signature_missing_header():
    body = b'{"hello": "world"}'
    assert verify_whatsapp_signature(body, None) is False
    assert verify_whatsapp_signature(body, "") is False


@pytest.mark.asyncio
async def test_meta_webhook_challenge_success():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN
            }
        )
        assert resp.status_code == 200
        assert resp.text == "1158201444"


@pytest.mark.asyncio
async def test_meta_webhook_challenge_invalid_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "wrong_token_here"
            }
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_webhook_rejected_on_bad_signature():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhook",
            content=b'{"some": "payload"}',
            headers={"X-Hub-Signature-256": "sha256=fake_signature"}
        )
        assert resp.status_code == 403
