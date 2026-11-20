"""
Observability and Prometheus metrics module for WhatsApp Agent Gateway.
Tracks webhook latencies, deduplication counts, rate-limiting,
flaky lab API retries, double-booking prevention, LLM token burn, and costs.
"""
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    REGISTRY
)

# Custom or default registry
registry = REGISTRY

# 1. Webhook Ingress Metrics
WEBHOOK_REQUESTS_TOTAL = Counter(
    "whatsapp_webhook_requests_total",
    "Total incoming WhatsApp webhook requests partitioned by status",
    ["status"],  # accepted, duplicate, rate_limited, invalid_signature, invalid_payload
    registry=registry
)

WEBHOOK_DURATION_SECONDS = Histogram(
    "whatsapp_webhook_duration_seconds",
    "Acknowledgment latency for incoming WhatsApp webhooks (target < 0.200s)",
    buckets=[0.010, 0.025, 0.050, 0.100, 0.150, 0.200, 0.500, 1.0, 2.0],
    registry=registry
)

# 2. Worker Processing Metrics
WORKER_JOBS_TOTAL = Counter(
    "worker_jobs_total",
    "Total jobs processed by asynchronous worker",
    ["status"],  # success, error, dead_letter
    registry=registry
)

WORKER_PROCESSING_DURATION_SECONDS = Histogram(
    "worker_processing_duration_seconds",
    "End-to-end processing duration for a WhatsApp message job",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
    registry=registry
)

# 3. Flaky Lab API & Retry Metrics
LAB_API_REQUESTS_TOTAL = Counter(
    "lab_api_requests_total",
    "Total requests to Upstream Lab API",
    ["endpoint", "status"],  # check_slots / book_test, success / failure
    registry=registry
)

LAB_API_RETRIES_TOTAL = Counter(
    "lab_api_retries_total",
    "Total retries triggered against flaky Lab API",
    ["endpoint"],
    registry=registry
)

# 4. Booking Integrity & Double-Booking Prevention Metrics
BOOKINGS_TOTAL = Counter(
    "bookings_total",
    "Total diagnostic test bookings handled by the gateway",
    ["status"],  # confirmed, double_booking_prevented, failed
    registry=registry
)

# 5. LLM Token & Cost Tracking
LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed across all conversations",
    ["type", "model"],  # type: prompt / completion
    registry=registry
)

LLM_COST_TOTAL = Counter(
    "llm_cost_total",
    "Total cumulative LLM financial cost incurred",
    ["currency"],  # USD / INR
    registry=registry
)

# 6. Human Escalation
HUMAN_HANDOFFS_TOTAL = Counter(
    "human_handoffs_total",
    "Total conversations handed off to human medical agents",
    ["priority"],  # LOW, MEDIUM, HIGH, URGENT
    registry=registry
)

ACTIVE_CONVERSATIONS = Gauge(
    "active_conversations_count",
    "Current active conversations in PostgreSQL",
    registry=registry
)


def get_prometheus_metrics() -> tuple[bytes, str]:
    """Return raw Prometheus metrics bytes and content type."""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def get_metrics_summary() -> dict:
    """
    Return a clean JSON-serializable dictionary summary of in-process counters.
    """
    summary = {
        "webhook": {
            "accepted": 0,
            "duplicate": 0,
            "rate_limited": 0,
            "invalid_signature": 0,
            "invalid_payload": 0,
        },
        "worker": {
            "success": 0,
            "error": 0,
        },
        "lab_api": {
            "requests_success": 0,
            "requests_failure": 0,
            "retries_total": 0,
        },
        "bookings": {
            "confirmed": 0,
            "double_booking_prevented": 0,
            "failed": 0,
        },
        "llm": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "cost_inr": 0.0,
        },
        "human_handoffs": {
            "total": 0
        }
    }

    try:
        for metric in registry.collect():
            for sample in metric.samples:
                name = sample.name
                labels = sample.labels
                value = sample.value

                if name == "whatsapp_webhook_requests_total":
                    st = labels.get("status", "")
                    if st in summary["webhook"]:
                        summary["webhook"][st] = int(value)
                elif name == "worker_jobs_total":
                    st = labels.get("status", "")
                    if st in summary["worker"]:
                        summary["worker"][st] = int(value)
                elif name == "lab_api_requests_total":
                    st = labels.get("status", "")
                    if st == "success":
                        summary["lab_api"]["requests_success"] += int(value)
                    elif st == "failure":
                        summary["lab_api"]["requests_failure"] += int(value)
                elif name == "lab_api_retries_total":
                    summary["lab_api"]["retries_total"] += int(value)
                elif name == "bookings_total":
                    st = labels.get("status", "")
                    if st in summary["bookings"]:
                        summary["bookings"][st] = int(value)
                elif name == "llm_tokens_total":
                    ttype = labels.get("type", "")
                    if ttype == "prompt":
                        summary["llm"]["prompt_tokens"] += int(value)
                    elif ttype == "completion":
                        summary["llm"]["completion_tokens"] += int(value)
                elif name == "llm_cost_total":
                    curr = labels.get("currency", "")
                    if curr == "USD":
                        summary["llm"]["cost_usd"] = round(float(value), 6)
                    elif curr == "INR":
                        summary["llm"]["cost_inr"] = round(float(value), 4)
                elif name == "human_handoffs_total":
                    summary["human_handoffs"]["total"] += int(value)

        summary["llm"]["total_tokens"] = summary["llm"]["prompt_tokens"] + summary["llm"]["completion_tokens"]
    except Exception:
        pass

    return summary


async def get_cluster_metrics_summary() -> dict:
    """
    Return comprehensive cluster-wide metrics combining:
    - Gateway Ingress & Latencies (from Prometheus in-memory registry)
    - Persistent Token & Cost Ledgers (from PostgreSQL)
    - Shared Worker Retries & Double-Booking Counters (from Redis)
    """
    summary = get_metrics_summary()

    try:
        from app.db import db
        from app.redis_client import redis_client

        # Query PostgreSQL single-source-of-truth
        db_stats = await db.get_cluster_aggregates()
        if db_stats:
            summary["worker"]["success"] = max(
                summary["worker"]["success"],
                db_stats.get("processed_jobs", 0)
            )
            summary["bookings"]["confirmed"] = max(
                summary["bookings"]["confirmed"],
                db_stats.get("confirmed_bookings", 0)
            )
            summary["human_handoffs"]["total"] = max(
                summary["human_handoffs"]["total"],
                db_stats.get("human_handoffs", 0)
            )
            summary["llm"]["prompt_tokens"] = max(
                summary["llm"]["prompt_tokens"],
                db_stats.get("prompt_tokens", 0)
            )
            summary["llm"]["completion_tokens"] = max(
                summary["llm"]["completion_tokens"],
                db_stats.get("completion_tokens", 0)
            )
            summary["llm"]["total_tokens"] = summary["llm"]["prompt_tokens"] + summary["llm"]["completion_tokens"]
            summary["llm"]["cost_usd"] = max(
                summary["llm"]["cost_usd"],
                db_stats.get("cost_usd", 0.0)
            )
            summary["llm"]["cost_inr"] = max(
                summary["llm"]["cost_inr"],
                db_stats.get("cost_inr", 0.0)
            )

        # Query Redis shared counters
        r_lab_succ = await redis_client.get_metric("lab:requests_success")
        r_lab_fail = await redis_client.get_metric("lab:requests_failure")
        r_lab_retries = await redis_client.get_metric("lab:retries")
        r_db_prevented = await redis_client.get_metric("bookings:double_booking_prevented")
        r_worker_succ = await redis_client.get_metric("worker:jobs_success")
        r_worker_err = await redis_client.get_metric("worker:jobs_error")

        if r_lab_succ:
            summary["lab_api"]["requests_success"] = max(summary["lab_api"]["requests_success"], r_lab_succ)
        if r_lab_fail:
            summary["lab_api"]["requests_failure"] = max(summary["lab_api"]["requests_failure"], r_lab_fail)
        if r_lab_retries:
            summary["lab_api"]["retries_total"] = max(summary["lab_api"]["retries_total"], r_lab_retries)
        if r_db_prevented:
            summary["bookings"]["double_booking_prevented"] = max(summary["bookings"]["double_booking_prevented"], r_db_prevented)
        if r_worker_succ:
            summary["worker"]["success"] = max(summary["worker"]["success"], r_worker_succ)
        if r_worker_err:
            summary["worker"]["error"] = max(summary["worker"]["error"], r_worker_err)

    except Exception:
        pass

    return summary


async def sync_cluster_metrics():
    """
    Sync PostgreSQL and Redis cluster metrics into Prometheus in-memory registry,
    ensuring /metrics exposes cluster-wide stats to Prometheus scraper.
    """
    try:
        summary = await get_cluster_metrics_summary()

        BOOKINGS_TOTAL.labels(status="confirmed")._value.set(summary["bookings"]["confirmed"])
        BOOKINGS_TOTAL.labels(status="double_booking_prevented")._value.set(summary["bookings"]["double_booking_prevented"])
        BOOKINGS_TOTAL.labels(status="failed")._value.set(summary["bookings"]["failed"])

        WORKER_JOBS_TOTAL.labels(status="success")._value.set(summary["worker"]["success"])
        WORKER_JOBS_TOTAL.labels(status="error")._value.set(summary["worker"]["error"])

        LAB_API_REQUESTS_TOTAL.labels(endpoint="lab", status="success")._value.set(summary["lab_api"]["requests_success"])
        LAB_API_REQUESTS_TOTAL.labels(endpoint="lab", status="failure")._value.set(summary["lab_api"]["requests_failure"])
        LAB_API_RETRIES_TOTAL.labels(endpoint="lab")._value.set(summary["lab_api"]["retries_total"])

        LLM_TOKENS_TOTAL.labels(type="prompt", model="cluster")._value.set(summary["llm"]["prompt_tokens"])
        LLM_TOKENS_TOTAL.labels(type="completion", model="cluster")._value.set(summary["llm"]["completion_tokens"])
        LLM_COST_TOTAL.labels(currency="USD")._value.set(summary["llm"]["cost_usd"])
        LLM_COST_TOTAL.labels(currency="INR")._value.set(summary["llm"]["cost_inr"])

        HUMAN_HANDOFFS_TOTAL.labels(priority="ALL")._value.set(summary["human_handoffs"]["total"])
    except Exception:
        pass

