"""
Pytest configuration and shared fixtures for unit and integration testing.
"""
import pytest
import asyncio
from app.config import settings
from app.redis_client import redis_client, InMemoryRedisMock
from app.db import db, InMemoryDBMock

# Ensure test settings
settings.ENVIRONMENT = "testing"
settings.WHATSAPP_APP_SECRET = "mock_secret_for_tests_only_3b9f"
settings.WHATSAPP_VERIFY_TOKEN = "heydoc_verify_token_2026"
settings.RATE_LIMIT_MAX_REQUESTS = 10
settings.RATE_LIMIT_WINDOW_SECONDS = 60
settings.RETRY_BASE_BACKOFF = 0.05
settings.RETRY_MAX_BACKOFF = 0.2


@pytest.fixture(autouse=True)
async def reset_test_state():
    """Reset in-memory storage before each test for clean isolation."""
    redis_client._client = InMemoryRedisMock()
    redis_client._is_mock = True
    db._mock_db = InMemoryDBMock()
    db._is_mock = True
    yield
