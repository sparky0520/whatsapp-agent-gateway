"""
Agent Tools Definition & Execution Layer.
Exposes:
1. check_slots(test_code, date)
2. book_test(patient_name, patient_phone, test_code, slot_time)
3. handoff_to_human(reason, priority)
Implements distributed locking and multi-layer concurrency control to guarantee zero double-booking.
"""
import uuid
import hashlib
import logging
from typing import Dict, Any, Optional, List
from app.agent.lab_client import lab_client, LabAPIError
from app.redis_client import redis_client
from app.db import db
from app.metrics import BOOKINGS_TOTAL, HUMAN_HANDOFFS_TOTAL

logger = logging.getLogger("agent_tools")

# OpenAI Function Calling Tool Definitions
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_slots",
            "description": "Check available appointment slots for a specific diagnostic medical test on a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_code": {
                        "type": "string",
                        "enum": ["CBC", "LIPID", "FBS", "THYROID", "COVID"],
                        "description": "Diagnostic test code (e.g. Complete Blood Count=CBC, Lipid Profile=LIPID, Fasting Blood Sugar=FBS)"
                    },
                    "date": {
                        "type": "string",
                        "description": "Target date in YYYY-MM-DD format (e.g. '2026-11-21')"
                    }
                },
                "required": ["test_code", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_test",
            "description": "Book a diagnostic test slot for the patient. Call after verifying slot availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "Full legal name of the patient"
                    },
                    "patient_phone": {
                        "type": "string",
                        "description": "WhatsApp phone number of the patient (e.g. '919876543210')"
                    },
                    "test_code": {
                        "type": "string",
                        "enum": ["CBC", "LIPID", "FBS", "THYROID", "COVID"],
                        "description": "Diagnostic test code"
                    },
                    "slot_time": {
                        "type": "string",
                        "description": "ISO-8601 formatted slot datetime (e.g. '2026-11-21T09:00:00Z')"
                    }
                },
                "required": ["patient_name", "patient_phone", "test_code", "slot_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_human",
            "description": "Transfer conversation to a human healthcare coordinator when the patient requests human help, has an emergency, or presents complex medical prep questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Detailed explanation for escalating to a human coordinator"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                        "description": "Urgency priority level"
                    }
                },
                "required": ["reason", "priority"]
            }
        }
    }
]


async def execute_check_slots(test_code: str, date: str) -> Dict[str, Any]:
    """Execute check_slots tool with resilient upstream query."""
    try:
        data = await lab_client.check_slots(test_code=test_code.upper().strip(), date=date.strip())
        available = [s["slot_time"] for s in data.get("available_slots", []) if s.get("available")]
        return {
            "status": "SUCCESS",
            "test_code": data.get("test_code"),
            "test_name": data.get("test_name"),
            "date": data.get("date"),
            "available_slots": available,
            "message": f"Found {len(available)} available slots for {data.get('test_name')} on {date}."
        }
    except LabAPIError as e:
        logger.error(f"Error checking slots: {e}")
        return {
            "status": "ERROR",
            "error": str(e),
            "message": f"Unable to fetch slots at the moment due to temporary lab system latency: {e}"
        }


async def execute_book_test(
    patient_name: str,
    patient_phone: str,
    test_code: str,
    slot_time: str,
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute book_test tool with absolute zero double-booking guarantee.
    Enforces:
    1. Deterministic Idempotency Key
    2. Redis Distributed Slot Mutex
    3. Flaky Lab API Booking Call (with backoff & jitter)
    4. PostgreSQL Relational Unique Constraint
    """
    code = test_code.upper().strip()
    slot = slot_time.strip()

    # Step 1: Generate stable idempotency key
    # Same user requesting same test & slot always produces identical idempotency key
    idempotency_raw = f"{patient_phone}:{code}:{slot}"
    idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

    # Step 2: Distributed lock over slot
    lock_token = await redis_client.acquire_slot_lock(code, slot, timeout_ms=12000)
    if not lock_token:
        # Another concurrent request is actively booking this exact slot!
        BOOKINGS_TOTAL.labels(status="double_booking_prevented").inc()
        try:
            await redis_client.incr_metric("bookings:double_booking_prevented")
        except Exception:
            pass
        return {
            "status": "CONFLICT",
            "error": "CONCURRENT_SLOT_LOCK",
            "message": f"Slot {slot} for {code} is currently being reserved by another patient. Please choose a different slot."
        }

    try:
        # Step 3: Call Lab API with idempotency key and exponential backoff
        try:
            lab_resp = await lab_client.book_slot(
                patient_name=patient_name,
                patient_phone=patient_phone,
                test_code=code,
                slot_time=slot,
                idempotency_key=idempotency_key
            )
            booking_ref = lab_resp.get("booking_reference") or f"BK-{uuid.uuid4().hex[:6].upper()}"
        except LabAPIError as lab_err:
            if lab_err.is_conflict:
                BOOKINGS_TOTAL.labels(status="double_booking_prevented").inc()
                try:
                    await redis_client.incr_metric("bookings:double_booking_prevented")
                except Exception:
                    pass
                return {
                    "status": "CONFLICT",
                    "error": "SLOT_ALREADY_BOOKED",
                    "message": f"Unfortunately, the {code} slot at {slot} is no longer available. Please select another time."
                }
            BOOKINGS_TOTAL.labels(status="failed").inc()
            return {
                "status": "ERROR",
                "error": str(lab_err),
                "message": f"We encountered a temporary issue with the lab booking system. Please try again or ask for a coordinator."
            }

        # Step 4: Persist in PostgreSQL with unique constraint guarantee
        success, db_status, record = await db.create_booking(
            booking_reference=booking_ref,
            conversation_id=conversation_id,
            patient_phone=patient_phone,
            patient_name=patient_name,
            test_code=code,
            slot_time=slot,
            idempotency_key=idempotency_key
        )

        if not success:
            BOOKINGS_TOTAL.labels(status="double_booking_prevented").inc()
            try:
                await redis_client.incr_metric("bookings:double_booking_prevented")
            except Exception:
                pass
            return {
                "status": "CONFLICT",
                "error": "DOUBLE_BOOKING_PREVENTED",
                "message": f"Slot {slot} for {code} is already booked in our records. Double-booking prevented."
            }

        BOOKINGS_TOTAL.labels(status="confirmed").inc()
        return {
            "status": "CONFIRMED",
            "booking_reference": booking_ref,
            "patient_name": patient_name,
            "test_code": code,
            "slot_time": slot,
            "message": f"Booking confirmed! Ref: {booking_ref}. Test: {code} on {slot}. Patient: {patient_name}."
        }

    finally:
        # Step 5: Always release the slot lock
        await redis_client.release_slot_lock(code, slot, lock_token)


async def execute_handoff_to_human(
    conversation_id: str,
    wa_id: str,
    reason: str,
    priority: str = "MEDIUM"
) -> Dict[str, Any]:
    """Execute handoff_to_human tool."""
    p_upper = priority.upper().strip()
    if p_upper not in ["LOW", "MEDIUM", "HIGH", "URGENT"]:
        p_upper = "MEDIUM"

    handoff_record = await db.create_human_handoff(
        conversation_id=conversation_id,
        wa_id=wa_id,
        reason=reason,
        priority=p_upper
    )
    HUMAN_HANDOFFS_TOTAL.labels(priority=p_upper).inc()

    # Conceal internal technical triage tags (MEDIUM, LOW) from patient-facing messages;
    # only expose urgency for critical / urgent medical alerts.
    priority_tag = f" ({p_upper} priority)" if p_upper in ["URGENT", "HIGH"] else ""

    return {
        "status": "HANDED_OFF",
        "handoff_id": handoff_record.get("id"),
        "priority": p_upper,
        "message": f"Conversation escalated to a human healthcare coordinator{priority_tag}. An agent will join shortly."
    }
