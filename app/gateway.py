"""
FastAPI WhatsApp Webhook Ingress Gateway.
Responsibilities:
1. Meta Webhook Verification (GET /webhook)
2. HMAC-SHA256 Signature Verification (X-Hub-Signature-256)
3. Per-phone Sliding Window Rate Limiting
4. Atomic Message Deduplication (wamid via Redis SET NX EX)
5. Ultra-fast sub-200ms Ingestion & Enqueueing
6. Prometheus Metrics Endpoint (/metrics) and JSON Summary (/metrics/summary)
"""
import time
import hmac
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Request, Response, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, JSONResponse
from app.config import settings
from app.models.schemas import WhatsAppWebhookPayload, IngestedMessageJob
from app.redis_client import redis_client
from app.db import db
from app.metrics import (
    WEBHOOK_REQUESTS_TOTAL,
    WEBHOOK_DURATION_SECONDS,
    get_prometheus_metrics,
    get_metrics_summary,
    get_cluster_metrics_summary
)

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("WhatsApp Gateway starting up...")
    await redis_client.get_client()
    await db.init_schema()
    yield
    await redis_client.close()
    await db.close()


app = FastAPI(title="WhatsApp Agent Gateway", version="1.0.0", lifespan=lifespan)


def verify_whatsapp_signature(body_bytes: bytes, signature_header: Optional[str]) -> bool:
    """
    Verify the X-Hub-Signature-256 header sent by Meta.
    Format: sha256={hex_digest}
    """
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected_sig = signature_header[len("sha256="):].strip()
    secret_bytes = settings.WHATSAPP_APP_SECRET.encode("utf-8")
    computed_sig = hmac.new(secret_bytes, body_bytes, hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "whatsapp-gateway",
        "environment": settings.ENVIRONMENT,
        "timestamp": time.time()
    }


@app.get("/metrics")
async def metrics():
    """Expose metrics in Prometheus text exposition format."""
    data, content_type = get_prometheus_metrics()
    return Response(content=data, media_type=content_type)


@app.get("/metrics/summary")
async def metrics_summary():
    """Expose JSON metrics summary for automated testing & monitoring scripts."""
    summary = await get_cluster_metrics_summary()
    return JSONResponse(content=summary)


@app.get("/api/conversations/{phone}")
async def get_conversation_details(phone: str):
    """Inspect conversation state, token counts, and spend for a given phone number."""
    conv = await db.get_conversation_by_wa_id(phone)
    if not conv:
        raise HTTPException(status_code=404, detail=f"No conversation found for phone {phone}")
    recent_msgs = await db.get_recent_messages(str(conv["id"]), limit=10)
    return {
        "conversation": conv,
        "recent_messages": recent_msgs
    }


@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token")
):
    """
    WhatsApp Cloud API Webhook Verification Endpoint.
    Meta makes a GET request when configuring webhook URLs in the App Dashboard.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Meta webhook verification challenge succeeded.")
        return PlainTextResponse(content=hub_challenge or "", status_code=status.HTTP_200_OK)
    
    logger.warning("Meta webhook verification failed: token mismatch.")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification token mismatch")


@app.post("/webhook")
async def handle_incoming_webhook(request: Request):
    """
    WhatsApp Cloud API Webhook Ingestion Endpoint.
    Strictly fulfills:
    1. HMAC-SHA256 signature verification (< 2ms)
    2. Per-sender phone rate limiting (< 2ms)
    3. Message deduplication via Redis SET NX EX (< 2ms)
    4. Async queue push (< 5ms)
    5. Acknowledgment response in UNDER 200 ms!
    """
    start_time = time.time()

    # Step 1: Read raw body and verify signature
    body_bytes = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not verify_whatsapp_signature(body_bytes, signature_header):
        WEBHOOK_REQUESTS_TOTAL.labels(status="invalid_signature").inc()
        duration = time.time() - start_time
        WEBHOOK_DURATION_SECONDS.observe(duration)
        logger.warning("Rejected webhook: Invalid or missing X-Hub-Signature-256")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid signature"
        )

    # Step 2: Parse JSON payload
    try:
        raw_json = json.loads(body_bytes.decode("utf-8"))
        payload = WhatsAppWebhookPayload.model_validate(raw_json)
    except Exception as parse_err:
        WEBHOOK_REQUESTS_TOTAL.labels(status="invalid_payload").inc()
        duration = time.time() - start_time
        WEBHOOK_DURATION_SECONDS.observe(duration)
        logger.warning(f"Malformed WhatsApp payload: {parse_err}")
        return JSONResponse(status_code=200, content={"status": "ignored", "reason": "invalid_payload"})

    # Step 3: Extract messages and enqueue
    enqueued_count = 0
    duplicate_count = 0
    rate_limited_count = 0

    for entry in payload.entry:
        for change in entry.changes:
            val = change.value
            if not val.messages:
                continue

            # Map contact profile name if available
            contact_map = {}
            if val.contacts:
                for c in val.contacts:
                    pname = c.profile.name if c.profile and c.profile.name else "Patient"
                    contact_map[c.wa_id] = pname

            for msg in val.messages:
                wamid = msg.id
                wa_id = msg.from_
                msg_text = msg.text.body if msg.text else ""
                sender_name = contact_map.get(wa_id, "Patient")

                # 3a. Rate Limit Check per sender phone
                is_allowed, count = await redis_client.check_rate_limit(wa_id)
                if not is_allowed:
                    WEBHOOK_REQUESTS_TOTAL.labels(status="rate_limited").inc()
                    rate_limited_count += 1
                    logger.warning(f"Rate limit exceeded for phone {wa_id} (count={count}). Dropping message {wamid}.")
                    continue

                # 3b. Atomic Deduplication Check (Redis SET NX EX)
                is_fresh = await redis_client.deduplicate_message(wamid)
                if not is_fresh:
                    # Message already seen and processed!
                    WEBHOOK_REQUESTS_TOTAL.labels(status="duplicate").inc()
                    duplicate_count += 1
                    logger.info(f"Duplicate message detected ({wamid}). Acknowledging without re-processing.")
                    continue

                # 3c. Valid fresh message -> Enqueue job for background worker
                job = IngestedMessageJob(
                    wamid=wamid,
                    wa_id=wa_id,
                    sender_name=sender_name,
                    text=msg_text,
                    timestamp=msg.timestamp,
                    received_at_epoch=time.time()
                )

                await redis_client.enqueue_job(job.model_dump_json())
                WEBHOOK_REQUESTS_TOTAL.labels(status="accepted").inc()
                enqueued_count += 1

    duration = time.time() - start_time
    WEBHOOK_DURATION_SECONDS.observe(duration)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "acknowledged",
            "enqueued": enqueued_count,
            "duplicates": duplicate_count,
            "rate_limited": rate_limited_count,
            "duration_ms": round(duration * 1000, 2)
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.GATEWAY_HOST, port=settings.GATEWAY_PORT)
