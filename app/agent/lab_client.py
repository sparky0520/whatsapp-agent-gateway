"""
Resilient HTTP Client for upstream Diagnostic Lab API.
Implements:
- Exponential Backoff with Full Jitter
- Automatic retry on 500, 502, 503, 504 and connection timeouts
- Non-retryable error handling (409 Conflict, 400 Bad Request)
- Prometheus metrics recording for requests and retries
"""
import asyncio
import random
import logging
from typing import Optional, Dict, Any
import httpx
from app.config import settings
from app.metrics import LAB_API_REQUESTS_TOTAL, LAB_API_RETRIES_TOTAL

logger = logging.getLogger("lab_client")


class LabAPIError(Exception):
    def __init__(self, message: str, status_code: int = 500, is_conflict: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.is_conflict = is_conflict


class ResilientLabClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        max_retries: int = settings.RETRY_MAX_ATTEMPTS,
        base_backoff: float = settings.RETRY_BASE_BACKOFF,
        max_backoff: float = settings.RETRY_MAX_BACKOFF,
        timeout: float = settings.LAB_API_TIMEOUT_SECONDS
    ):
        self.base_url = (base_url or settings.LAB_API_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Exponential backoff with full jitter:
        delay = uniform(0, min(max_backoff, base_backoff * (2 ** attempt)))
        """
        temp = min(self.max_backoff, self.base_backoff * (2 ** attempt))
        return random.uniform(0.1, temp)

    async def check_slots(self, test_code: str, date: str) -> Dict[str, Any]:
        """Query available slots with retry resilience."""
        url = f"{self.base_url}/lab/slots"
        params = {"test_code": test_code, "date": date}
        return await self._execute_with_retry("GET", url, params=params, endpoint_name="check_slots")

    async def book_slot(
        self,
        patient_name: str,
        patient_phone: str,
        test_code: str,
        slot_time: str,
        idempotency_key: str
    ) -> Dict[str, Any]:
        """Book a slot with retry resilience and idempotency."""
        url = f"{self.base_url}/lab/book"
        payload = {
            "patient_name": patient_name,
            "patient_phone": patient_phone,
            "test_code": test_code,
            "slot_time": slot_time,
            "idempotency_key": idempotency_key
        }
        headers = {"X-Idempotency-Key": idempotency_key}
        return await self._execute_with_retry(
            "POST", url, json_data=payload, headers=headers, endpoint_name="book_test"
        )

    async def _execute_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        endpoint_name: str = "unknown"
    ) -> Dict[str, Any]:
        client = self._get_client()

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers
                )

                # Success
                if response.status_code == 200:
                    LAB_API_REQUESTS_TOTAL.labels(endpoint=endpoint_name, status="success").inc()
                    try:
                        from app.redis_client import redis_client
                        await redis_client.incr_metric("lab:requests_success")
                    except Exception:
                        pass
                    return response.json()

                # Conflict (Slot already booked) - Do NOT retry!
                if response.status_code == 409:
                    LAB_API_REQUESTS_TOTAL.labels(endpoint=endpoint_name, status="conflict").inc()
                    error_data = response.json() if response.content else {}
                    msg = error_data.get("message", "Slot already booked")
                    raise LabAPIError(msg, status_code=409, is_conflict=True)

                # Bad Request (Client error) - Do NOT retry
                if 400 <= response.status_code < 500:
                    LAB_API_REQUESTS_TOTAL.labels(endpoint=endpoint_name, status="bad_request").inc()
                    raise LabAPIError(f"Client error from lab: {response.text}", status_code=response.status_code)

                # Server error (500, 502, 503, 504) -> Candidate for retry
                LAB_API_REQUESTS_TOTAL.labels(endpoint=endpoint_name, status="failure").inc()
                try:
                    from app.redis_client import redis_client
                    await redis_client.incr_metric("lab:requests_failure")
                except Exception:
                    pass
                if attempt < self.max_retries:
                    LAB_API_RETRIES_TOTAL.labels(endpoint=endpoint_name).inc()
                    try:
                        from app.redis_client import redis_client
                        await redis_client.incr_metric("lab:retries")
                    except Exception:
                        pass
                    backoff = self._calculate_backoff(attempt)
                    logger.warning(
                        f"LabClient: Request to {url} failed with {response.status_code}. "
                        f"Retrying attempt {attempt + 1}/{self.max_retries} in {backoff:.2f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                else:
                    raise LabAPIError(
                        f"Lab API failed after {self.max_retries} retries (Status {response.status_code})",
                        status_code=response.status_code
                    )

            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
                LAB_API_REQUESTS_TOTAL.labels(endpoint=endpoint_name, status="network_error").inc()
                if attempt < self.max_retries:
                    LAB_API_RETRIES_TOTAL.labels(endpoint=endpoint_name).inc()
                    backoff = self._calculate_backoff(attempt)
                    logger.warning(
                        f"LabClient: Network error ({type(exc).__name__}) connecting to {url}. "
                        f"Retrying attempt {attempt + 1}/{self.max_retries} in {backoff:.2f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                else:
                    raise LabAPIError(
                        f"Lab API network failure after {self.max_retries} retries: {exc}",
                        status_code=503
                    )

        raise LabAPIError("Max retries exceeded without successful response")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


lab_client = ResilientLabClient()
