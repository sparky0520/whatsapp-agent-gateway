"""
Resilience & Retry Tests: Flaky Lab API client with exponential backoff & full jitter.
"""
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from app.agent.lab_client import ResilientLabClient, LabAPIError


@pytest.mark.asyncio
async def test_flaky_api_recovers_after_transient_failures():
    """Client encounters two 503 errors, then succeeds on 3rd attempt."""
    client = ResilientLabClient(
        base_url="http://mock-lab:8001",
        max_retries=3,
        base_backoff=0.01,
        max_backoff=0.05
    )

    attempt_counter = 0

    async def mock_request(method, url, **kwargs):
        nonlocal attempt_counter
        attempt_counter += 1
        if attempt_counter < 3:
            # First 2 attempts fail with 503 Service Unavailable
            return httpx.Response(503, text="Service Unavailable", request=httpx.Request("GET", url))
        # 3rd attempt succeeds
        return httpx.Response(200, json={"test_code": "CBC", "available_slots": []}, request=httpx.Request("GET", url))

    with patch.object(client._get_client(), "request", side_effect=mock_request):
        result = await client.check_slots("CBC", "2026-11-21")
        assert result["test_code"] == "CBC"
        assert attempt_counter == 3


@pytest.mark.asyncio
async def test_conflict_409_is_not_retried():
    """Client receives 409 Conflict (Slot Taken). It must fail immediately without retrying."""
    client = ResilientLabClient(
        base_url="http://mock-lab:8001",
        max_retries=3,
        base_backoff=0.01
    )

    call_count = 0

    async def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            409,
            json={"status": "FAILED", "message": "Slot already booked"},
            request=httpx.Request("POST", url)
        )

    with patch.object(client._get_client(), "request", side_effect=mock_request):
        with pytest.raises(LabAPIError) as exc_info:
            await client.book_slot("Aarav", "919876543210", "CBC", "2026-11-21T09:00:00Z", "idemp_1")

        assert exc_info.value.is_conflict is True
        assert exc_info.value.status_code == 409
        # Invariant: 409 conflict was called exactly ONCE; no wasteful retries!
        assert call_count == 1


@pytest.mark.asyncio
async def test_persistent_failure_raises_after_max_retries():
    """Client encounters continuous 500 errors and exhausts all retries."""
    max_retries = 2
    client = ResilientLabClient(
        base_url="http://mock-lab:8001",
        max_retries=max_retries,
        base_backoff=0.01
    )

    call_count = 0

    async def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, text="Internal Error", request=httpx.Request("GET", url))

    with patch.object(client._get_client(), "request", side_effect=mock_request):
        with pytest.raises(LabAPIError) as exc_info:
            await client.check_slots("CBC", "2026-11-21")

        assert exc_info.value.status_code == 500
        assert call_count == max_retries + 1
