"""
Unit tests for per-phone sliding window rate limiting.
"""
import pytest
from app.redis_client import redis_client


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_threshold():
    phone = "919876500001"
    max_reqs = 5

    for i in range(max_reqs):
        allowed, count = await redis_client.check_rate_limit(phone, max_requests=max_reqs, window_seconds=10)
        assert allowed is True
        assert count == i + 1


@pytest.mark.asyncio
async def test_rate_limiter_blocks_above_threshold():
    phone = "919876500002"
    max_reqs = 3

    for _ in range(max_reqs):
        allowed, _ = await redis_client.check_rate_limit(phone, max_requests=max_reqs, window_seconds=10)
        assert allowed is True

    # 4th request exceeds max_reqs=3
    allowed, count = await redis_client.check_rate_limit(phone, max_requests=max_reqs, window_seconds=10)
    assert allowed is False
    assert count >= max_reqs


@pytest.mark.asyncio
async def test_rate_limiter_isolated_per_phone():
    phone_a = "919876500003"
    phone_b = "919876500004"
    max_reqs = 2

    # Exhaust phone A
    await redis_client.check_rate_limit(phone_a, max_requests=max_reqs, window_seconds=10)
    await redis_client.check_rate_limit(phone_a, max_requests=max_reqs, window_seconds=10)
    allowed_a, _ = await redis_client.check_rate_limit(phone_a, max_requests=max_reqs, window_seconds=10)
    assert allowed_a is False

    # Phone B should still be allowed
    allowed_b, _ = await redis_client.check_rate_limit(phone_b, max_requests=max_reqs, window_seconds=10)
    assert allowed_b is True
