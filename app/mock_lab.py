"""
Mock Diagnostic Lab API Server.
Simulates a flaky, high-latency third-party diagnostic laboratory service:
- 20% transient failures (HTTP 500 / 503)
- Simulated latency (500ms - 1500ms)
- Enforces strict idempotency via idempotency_key
- Enforces zero double-booking on slots (HTTP 409 Conflict)
"""
import asyncio
import random
import uuid
import logging
from typing import Dict, Any, Optional
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from app.config import settings
from app.models.schemas import CheckSlotsResponse, SlotItem, BookTestRequest, BookTestResponse

logger = logging.getLogger("mock_lab")

app = FastAPI(title="Mock Diagnostic Lab API", version="1.0.0")

# In-memory inventory of slots and bookings
TEST_CATALOG = {
    "CBC": "Complete Blood Count",
    "LIPID": "Lipid Profile",
    "FBS": "Fasting Blood Sugar",
    "THYROID": "Thyroid Profile",
    "COVID": "RT-PCR Covid Test"
}

STANDARD_HOURS = ["08:00:00Z", "09:00:00Z", "10:00:00Z", "11:00:00Z", "14:00:00Z", "16:00:00Z"]

# State store
booked_slots: set[tuple[str, str]] = set()  # (test_code, slot_time)
idempotent_bookings: Dict[str, dict] = {}   # idempotency_key -> booking_dict
all_bookings: Dict[str, dict] = {}          # booking_reference -> booking_dict

# Tunable simulation controls
SIMULATION_CONFIG = {
    "failure_rate": settings.LAB_FAILURE_RATE,
    "min_latency_ms": settings.LAB_MIN_LATENCY_MS,
    "max_latency_ms": settings.LAB_MAX_LATENCY_MS,
    "enabled": True
}


async def _simulate_latency_and_flakiness(endpoint_name: str):
    """Inject artificial latency and random 20% transient failures."""
    if not SIMULATION_CONFIG["enabled"]:
        return

    # 1. Latency injection
    min_lat = SIMULATION_CONFIG["min_latency_ms"]
    max_lat = SIMULATION_CONFIG["max_latency_ms"]
    if max_lat > 0:
        delay = random.uniform(min_lat, max_lat) / 1000.0
        await asyncio.sleep(delay)

    # 2. Transient failure injection (20% default)
    fail_prob = SIMULATION_CONFIG["failure_rate"]
    if random.random() < fail_prob:
        status_code = random.choice([500, 503])
        logger.warning(f"MockLab [{endpoint_name}]: Injecting simulated {status_code} transient failure")
        raise HTTPException(
            status_code=status_code,
            detail=f"Simulated transient error from upstream lab server ({status_code})"
        )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock_lab"}


@app.post("/lab/configure")
async def configure_simulation(
    failure_rate: Optional[float] = None,
    min_latency_ms: Optional[int] = None,
    max_latency_ms: Optional[int] = None,
    enabled: Optional[bool] = None
):
    """Adjust simulation knobs dynamically for unit tests vs stress testing."""
    if failure_rate is not None:
        SIMULATION_CONFIG["failure_rate"] = failure_rate
    if min_latency_ms is not None:
        SIMULATION_CONFIG["min_latency_ms"] = min_latency_ms
    if max_latency_ms is not None:
        SIMULATION_CONFIG["max_latency_ms"] = max_latency_ms
    if enabled is not None:
        SIMULATION_CONFIG["enabled"] = enabled
    return {"status": "configured", "current_config": SIMULATION_CONFIG}


@app.post("/lab/reset")
async def reset_state():
    """Reset all bookings and inventory (useful for test suites)."""
    booked_slots.clear()
    idempotent_bookings.clear()
    all_bookings.clear()
    return {"status": "reset", "active_bookings": 0}


@app.get("/lab/slots", response_model=CheckSlotsResponse)
async def check_slots(
    test_code: str = Query(..., description="Test code, e.g. CBC, LIPID, FBS"),
    date: str = Query(..., description="Date in YYYY-MM-DD format")
):
    """Check available diagnostic appointment slots on a given date."""
    code = test_code.upper().strip()
    if code not in TEST_CATALOG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown test code '{code}'. Available: {list(TEST_CATALOG.keys())}"
        )

    await _simulate_latency_and_flakiness("check_slots")

    # Generate slots for standard hours
    slots = []
    for hour in STANDARD_HOURS:
        iso_time = f"{date}T{hour}"
        is_booked = (code, iso_time) in booked_slots
        slots.append(SlotItem(slot_time=iso_time, available=not is_booked))

    return CheckSlotsResponse(
        test_code=code,
        test_name=TEST_CATALOG[code],
        date=date,
        available_slots=slots
    )


@app.post("/lab/book", response_model=BookTestResponse)
async def book_test(
    request: BookTestRequest,
    x_idempotency_key: Optional[str] = Header(None)
):
    """
    Book a diagnostic test slot.
    Guarantees:
    - Retries with the same idempotency_key return the original booking (Idempotency).
    - If the slot is already taken, returns HTTP 409 Conflict (Zero Double-Booking).
    - Injects 20% transient failures to test retry resilience.
    """
    idempotency_key = request.idempotency_key or x_idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing required idempotency_key")

    # 1. Idempotency Check BEFORE simulated flakiness
    # If the lab already successfully processed this exact idempotency key, return it idempotently!
    if idempotency_key in idempotent_bookings:
        existing = idempotent_bookings[idempotency_key]
        return BookTestResponse(
            status="CONFIRMED",
            booking_reference=existing["booking_reference"],
            patient_name=existing["patient_name"],
            patient_phone=existing["patient_phone"],
            test_code=existing["test_code"],
            slot_time=existing["slot_time"],
            message="Idempotent response: booking already confirmed."
        )

    # 2. Inject flakiness & latency
    await _simulate_latency_and_flakiness("book_test")

    code = request.test_code.upper().strip()
    slot_time = request.slot_time.strip()

    # 3. Double-Booking Protection Check
    slot_key = (code, slot_time)
    if slot_key in booked_slots:
        logger.warning(f"MockLab: Conflict! Slot {code} at {slot_time} is already booked.")
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "FAILED",
                "error": "SLOT_ALREADY_BOOKED",
                "message": f"Slot {slot_time} for test {code} is already booked.",
                "patient_name": request.patient_name,
                "patient_phone": request.patient_phone,
                "test_code": code,
                "slot_time": slot_time
            }
        )

    # 4. Reserve Slot
    ref_suffix = uuid.uuid4().hex[:6].upper()
    booking_ref = f"BK-{ref_suffix}"
    booking_record = {
        "booking_reference": booking_ref,
        "patient_name": request.patient_name,
        "patient_phone": request.patient_phone,
        "test_code": code,
        "slot_time": slot_time,
        "idempotency_key": idempotency_key,
        "created_at": datetime.utcnow().isoformat()
    }

    booked_slots.add(slot_key)
    idempotent_bookings[idempotency_key] = booking_record
    all_bookings[booking_ref] = booking_record

    logger.info(f"MockLab: Confirmed booking {booking_ref} for {request.patient_name} ({code} at {slot_time})")

    return BookTestResponse(
        status="CONFIRMED",
        booking_reference=booking_ref,
        patient_name=request.patient_name,
        patient_phone=request.patient_phone,
        test_code=code,
        slot_time=slot_time,
        message="Test appointment confirmed successfully."
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.MOCK_LAB_HOST, port=settings.MOCK_LAB_PORT)
