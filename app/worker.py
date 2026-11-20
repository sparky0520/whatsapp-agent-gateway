"""
Asynchronous Background Worker for WhatsApp Agent Gateway.
Responsibilities:
1. Dequeues ingested WhatsApp messages from Redis.
2. Manages conversational state machine in PostgreSQL.
3. Invokes LLM with sliding context window and tool execution:
   - check_slots
   - book_test (with distributed locking & zero double-booking)
   - handoff_to_human
4. Persists tokens, costs, and dialogue history to PostgreSQL.
5. Emits Prometheus metrics for latency and job status.
"""
import asyncio
import json
import logging
import signal
import time
from typing import Optional
from app.config import settings
from app.redis_client import redis_client
from app.db import db
from app.models.schemas import IngestedMessageJob
from app.agent.llm import llm_client
from app.agent.tools import (
    execute_check_slots,
    execute_book_test,
    execute_handoff_to_human
)
from app.metrics import (
    WORKER_JOBS_TOTAL,
    WORKER_PROCESSING_DURATION_SECONDS
)

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger("worker")

_is_running = True


def handle_shutdown(signum, frame):
    global _is_running
    logger.info("Worker received shutdown signal. Stopping gracefully...")
    _is_running = False


async def process_job(job_data: str):
    """Process a single ingested WhatsApp message job."""
    start_time = time.time()
    try:
        data = json.loads(job_data)
        job = IngestedMessageJob.model_validate(data)

        # 1. Fetch or create conversation in PostgreSQL
        conv = await db.get_or_create_conversation(wa_id=job.wa_id, patient_name=job.sender_name)
        conv_id = str(conv["id"])

        # If conversation is already handed off, automated bot does not interfere
        if conv.get("status") == "HANDED_OFF":
            logger.info(f"Conversation {conv_id} is marked HANDED_OFF. Logging message for human agent.")
            await db.log_message(
                conversation_id=conv_id,
                role="user",
                content=job.text,
                wamid=job.wamid
            )
            WORKER_JOBS_TOTAL.labels(status="success").inc()
            await redis_client.incr_metric("worker:jobs_success")
            return

        # 2. Log incoming user message
        await db.log_message(
            conversation_id=conv_id,
            role="user",
            content=job.text,
            wamid=job.wamid
        )

        # 3. Load recent conversation history (sliding window: 6 turns)
        history = await db.get_recent_messages(conversation_id=conv_id, limit=6)
        formatted_messages = []
        for h in history:
            msg_dict = {"role": h["role"], "content": h.get("content") or ""}
            if h.get("tool_calls"):
                msg_dict["tool_calls"] = h["tool_calls"]
            if h.get("tool_call_id"):
                msg_dict["tool_call_id"] = h["tool_call_id"]
            formatted_messages.append(msg_dict)

        # 4. Invoke LLM with tools
        llm_resp = await llm_client.chat(
            messages=formatted_messages,
            patient_name=job.sender_name,
            patient_phone=job.wa_id
        )

        # 5. Handle Tool Calls if any
        assistant_reply = llm_resp.content or ""
        executed_tool_calls = []

        if llm_resp.tool_calls:
            for tc in llm_resp.tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name")
                args = {}
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    pass

                logger.info(f"Worker: Invoking tool '{fn_name}' with args {args} for conv {conv_id}")
                tool_result = {}

                if fn_name == "check_slots":
                    test_code = args.get("test_code", "CBC")
                    date = args.get("date", "2026-11-21")
                    tool_result = await execute_check_slots(test_code, date)
                    assistant_reply = tool_result.get("message", assistant_reply)

                elif fn_name == "book_test":
                    pname = args.get("patient_name") or job.sender_name
                    pphone = args.get("patient_phone") or job.wa_id
                    tcode = args.get("test_code", "CBC")
                    stime = args.get("slot_time", "2026-11-21T10:00:00Z")
                    tool_result = await execute_book_test(
                        patient_name=pname,
                        patient_phone=pphone,
                        test_code=tcode,
                        slot_time=stime,
                        conversation_id=conv_id
                    )
                    assistant_reply = tool_result.get("message", assistant_reply)

                elif fn_name == "handoff_to_human":
                    reason = args.get("reason", "Patient escalation")
                    priority = args.get("priority", "MEDIUM")
                    tool_result = await execute_handoff_to_human(
                        conversation_id=conv_id,
                        wa_id=job.wa_id,
                        reason=reason,
                        priority=priority
                    )
                    assistant_reply = tool_result.get("message", assistant_reply)

                executed_tool_calls.append({
                    "id": tc.get("id"),
                    "name": fn_name,
                    "result": tool_result
                })

        # 6. Log Assistant Message to PostgreSQL
        await db.log_message(
            conversation_id=conv_id,
            role="assistant",
            content=assistant_reply,
            tool_calls=executed_tool_calls if executed_tool_calls else None,
            prompt_tokens=llm_resp.prompt_tokens,
            completion_tokens=llm_resp.completion_tokens,
            cost_usd=llm_resp.cost_usd
        )

        # 7. Update Conversation Token & Cost Aggregates
        await db.update_conversation_metrics(
            conversation_id=conv_id,
            prompt_tokens=llm_resp.prompt_tokens,
            completion_tokens=llm_resp.completion_tokens,
            cost_usd=llm_resp.cost_usd,
            cost_inr=llm_resp.cost_inr
        )

        duration = time.time() - start_time
        WORKER_PROCESSING_DURATION_SECONDS.observe(duration)
        WORKER_JOBS_TOTAL.labels(status="success").inc()
        await redis_client.incr_metric("worker:jobs_success")
        logger.info(
            f"Successfully processed message {job.wamid} for {job.wa_id} in {duration:.3f}s. "
            f"Tokens: {llm_resp.prompt_tokens + llm_resp.completion_tokens}, Cost: ${llm_resp.cost_usd:.6f}"
        )

    except Exception as e:
        duration = time.time() - start_time
        WORKER_PROCESSING_DURATION_SECONDS.observe(duration)
        WORKER_JOBS_TOTAL.labels(status="error").inc()
        await redis_client.incr_metric("worker:jobs_error")
        logger.error(f"Failed to process job ({e})", exc_info=True)


async def run_worker():
    """Main worker event loop."""
    logger.info("WhatsApp Agent Worker started, waiting for jobs in Redis...")
    await redis_client.get_client()
    await db.init_schema()

    while _is_running:
        try:
            job_data = await redis_client.dequeue_job(timeout=1)
            if job_data:
                await process_job(job_data)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in worker queue loop: {e}", exc_info=True)
            await asyncio.sleep(0.5)

    logger.info("Worker gracefully stopped.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    asyncio.run(run_worker())
