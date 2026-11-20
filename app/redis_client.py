"""
Async Redis Client providing:
1. Webhook Message Deduplication (atomic SET NX EX)
2. Per-phone Sliding Window Rate Limiting
3. Distributed Locking for Slot Reservation Concurrency
4. Queue Broker (enqueue / dequeue)
Includes an in-memory fallback for unit tests and local runs without Redis.
"""
import time
import uuid
import logging
from typing import Optional, Tuple
import redis.asyncio as aioredis
from app.config import settings

logger = logging.getLogger("redis_client")


class InMemoryRedisMock:
    """Fallback in-memory mock for testing environments without Redis server."""
    def __init__(self):
        self._store = {}
        self._expires = {}
        self._queues = {}
        self._zsets = {}

    def _cleanup(self, key: str):
        if key in self._expires and time.time() > self._expires[key]:
            self._store.pop(key, None)
            self._expires.pop(key, None)
            self._zsets.pop(key, None)

    async def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False) -> bool:
        self._cleanup(key)
        if nx and key in self._store:
            return False
        self._store[key] = value
        if ex:
            self._expires[key] = time.time() + ex
        return True

    async def get(self, key: str) -> Optional[str]:
        self._cleanup(key)
        return self._store.get(key)

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._expires.pop(k, None)
                count += 1
        return count

    async def rpush(self, key: str, value: str) -> int:
        if key not in self._queues:
            self._queues[key] = []
        self._queues[key].append(value)
        return len(self._queues[key])

    async def blpop(self, keys: list[str], timeout: int = 1) -> Optional[Tuple[str, str]]:
        start = time.time()
        key = keys[0] if isinstance(keys, list) else keys
        while time.time() - start < timeout:
            if key in self._queues and len(self._queues[key]) > 0:
                val = self._queues[key].pop(0)
                return (key, val)
            import asyncio
            await asyncio.sleep(0.05)
        return None

    async def incrby(self, key: str, amount: int = 1) -> int:
        current = int(self._store.get(key, "0"))
        new_val = current + amount
        self._store[key] = str(new_val)
        return new_val

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        if key not in self._zsets:
            self._zsets[key] = []
        added = 0
        for member, score in mapping.items():
            self._zsets[key].append((score, member))
            added += 1
        return added

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        if key not in self._zsets:
            return 0
        original_len = len(self._zsets[key])
        self._zsets[key] = [(s, m) for s, m in self._zsets[key] if not (min_score <= s <= max_score)]
        return original_len - len(self._zsets[key])

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, []))

    async def expire(self, key: str, seconds: int):
        self._expires[key] = time.time() + seconds

    async def ping(self) -> bool:
        return True

    async def close(self):
        pass


class RedisClient:
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self._client = None
        self._is_mock = False

    async def get_client(self):
        if self._client is not None:
            return self._client
        try:
            client = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=2.0
            )
            await client.ping()
            self._client = client
            self._is_mock = False
            logger.info("Connected to Redis successfully.")
        except Exception as e:
            logger.warning(f"Could not connect to Redis at {self.redis_url} ({e}). Using in-memory Redis mock.")
            self._client = InMemoryRedisMock()
            self._is_mock = True
        return self._client

    async def deduplicate_message(self, wamid: str, ttl_seconds: int = settings.DEDUP_TTL_SECONDS) -> bool:
        """
        Atomically check and record a WhatsApp message ID (wamid).
        Returns True if the message is fresh (not seen before).
        Returns False if the message is a DUPLICATE (already seen).
        """
        client = await self.get_client()
        key = f"dedup:msg:{wamid}"
        # SET key 1 EX ttl NX returns True if set, None/False if key already existed
        is_fresh = await client.set(key, "1", ex=ttl_seconds, nx=True)
        return bool(is_fresh)

    async def check_rate_limit(
        self,
        wa_id: str,
        max_requests: int = settings.RATE_LIMIT_MAX_REQUESTS,
        window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS
    ) -> Tuple[bool, int]:
        """
        Sliding-window rate limiter per phone number (wa_id).
        Returns (is_allowed: bool, current_request_count: int).
        """
        if not settings.RATE_LIMIT_ENABLED:
            return True, 0

        client = await self.get_client()
        now = time.time()
        window_start = now - window_seconds
        key = f"ratelimit:{wa_id}"

        # Remove elements older than sliding window
        await client.zremrangebyscore(key, 0, window_start)
        # Count current requests in this window
        count = await client.zcard(key)

        if count >= max_requests:
            return False, count

        # Record this request
        unique_member = f"{now}:{uuid.uuid4().hex[:8]}"
        await client.zadd(key, {unique_member: now})
        await client.expire(key, window_seconds + 5)
        return True, count + 1

    async def acquire_slot_lock(
        self,
        test_code: str,
        slot_time: str,
        timeout_ms: int = 15000
    ) -> Optional[str]:
        """
        Acquire a distributed lock for reserving a specific slot.
        Returns lock_token on success, or None if already locked.
        """
        client = await self.get_client()
        key = f"lock:slot:{test_code}:{slot_time}"
        token = str(uuid.uuid4())
        timeout_seconds = max(1, timeout_ms // 1000)

        acquired = await client.set(key, token, ex=timeout_seconds, nx=True)
        return token if acquired else None

    async def release_slot_lock(self, test_code: str, slot_time: str, token: str) -> bool:
        """
        Safely release the slot lock only if the token matches.
        """
        client = await self.get_client()
        key = f"lock:slot:{test_code}:{slot_time}"
        current = await client.get(key)
        if current == token:
            await client.delete(key)
            return True
        return False

    async def enqueue_job(self, payload_json: str, queue_name: str = settings.MESSAGE_QUEUE_NAME) -> int:
        """Push a message job into the Redis queue."""
        client = await self.get_client()
        return await client.rpush(queue_name, payload_json)

    async def dequeue_job(self, queue_name: str = settings.MESSAGE_QUEUE_NAME, timeout: int = 2) -> Optional[str]:
        """Pop a message job from the Redis queue with a timeout."""
        client = await self.get_client()
        res = await client.blpop([queue_name], timeout=timeout)
        if res:
            return res[1]
        return None

    async def incr_metric(self, name: str, amount: int = 1) -> int:
        """Increment a shared Redis counter across workers and gateways."""
        client = await self.get_client()
        key = f"metric:{name}"
        if self._is_mock:
            return await client.incrby(key, amount)
        return await client.incrby(key, amount)

    async def get_metric(self, name: str) -> int:
        """Get the current value of a shared Redis counter."""
        client = await self.get_client()
        key = f"metric:{name}"
        val = await client.get(key)
        return int(val) if val else 0

    async def close(self):
        if self._client and not self._is_mock:
            await self._client.close()


redis_client = RedisClient()
