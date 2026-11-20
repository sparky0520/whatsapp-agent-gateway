"""
Concurrency & Invariant Tests: Zero Double-Booking Guarantee.
Verifies that under heavy concurrent race conditions and retries,
only a single patient can book any specific time slot.
"""
import pytest
import asyncio
from app.db import db
from app.agent.tools import execute_book_test
from app.agent.lab_client import lab_client


@pytest.mark.asyncio
async def test_single_slot_double_booking_prevention_direct_db():
    """Verify relational database constraint stops duplicate booking for same slot."""
    slot_time = "2026-11-21T10:00:00Z"
    test_code = "CBC"

    # First booking succeeds
    ok1, status1, rec1 = await db.create_booking(
        booking_reference="BK-001",
        conversation_id=None,
        patient_phone="919876543201",
        patient_name="Patient 1",
        test_code=test_code,
        slot_time=slot_time,
        idempotency_key="idemp_key_001"
    )
    assert ok1 is True
    assert status1 == "CONFIRMED"

    # Second booking for the same test & slot MUST fail!
    ok2, status2, rec2 = await db.create_booking(
        booking_reference="BK-002",
        conversation_id=None,
        patient_phone="919876543202",
        patient_name="Patient 2",
        test_code=test_code,
        slot_time=slot_time,
        idempotency_key="idemp_key_002"
    )
    assert ok2 is False
    assert status2 == "DOUBLE_BOOKING_PREVENTED"
    assert rec2 is None


@pytest.mark.asyncio
async def test_idempotent_booking_retry_succeeds_without_double_allocation():
    """Verify that retrying with the same idempotency key returns the original booking."""
    slot_time = "2026-11-21T11:00:00Z"
    test_code = "LIPID"
    key = "idemp_stable_123"

    ok1, status1, rec1 = await db.create_booking(
        booking_reference="BK-100",
        conversation_id=None,
        patient_phone="919876543210",
        patient_name="Aarav Sharma",
        test_code=test_code,
        slot_time=slot_time,
        idempotency_key=key
    )
    assert ok1 is True

    # Retry with same idempotency key
    ok2, status2, rec2 = await db.create_booking(
        booking_reference="BK-100",
        conversation_id=None,
        patient_phone="919876543210",
        patient_name="Aarav Sharma",
        test_code=test_code,
        slot_time=slot_time,
        idempotency_key=key
    )
    assert ok2 is True
    assert status2 == "IDEMPOTENT_CONFIRMATION"
    assert rec2["booking_reference"] == "BK-100"


@pytest.mark.asyncio
async def test_concurrent_tool_book_test_race_condition(monkeypatch):
    """
    Simulate 10 concurrent requests simultaneously attempting to book
    the exact same test slot via the agent execute_book_test tool.
    Invariant: Exactly 1 succeeds, 9 fail with conflict.
    """
    # Mock lab client to return a booking reference without external network
    async def mock_book_slot(patient_name, patient_phone, test_code, slot_time, idempotency_key):
        return {"status": "CONFIRMED", "booking_reference": f"BK-TEST-{patient_phone[-4:]}"}

    monkeypatch.setattr(lab_client, "book_slot", mock_book_slot)

    target_slot = "2026-11-21T14:00:00Z"
    target_test = "FBS"

    async def book_attempt(patient_idx: int):
        phone = f"9198765432{patient_idx:02d}"
        name = f"Patient {patient_idx}"
        return await execute_book_test(
            patient_name=name,
            patient_phone=phone,
            test_code=target_test,
            slot_time=target_slot
        )

    # Launch 10 simultaneous booking attempts
    results = await asyncio.gather(*[book_attempt(i) for i in range(10)])

    confirmed_count = sum(1 for r in results if r.get("status") == "CONFIRMED")
    conflict_count = sum(1 for r in results if r.get("status") == "CONFLICT")

    # Invariant: EXACTLY 1 booking confirmed, rest rejected!
    assert confirmed_count == 1
    assert conflict_count == 9
