"""
PostgreSQL asynchronous database client using asyncpg.
Handles conversation state, message audit logging, token & cost tracking,
and relational guarantees against double-booking.
Includes an in-memory fallback for isolated testing.
"""
import uuid
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import asyncpg
from app.config import settings

logger = logging.getLogger("db")


class InMemoryDBMock:
    """In-memory relational database mock for standalone testing without Postgres."""
    def __init__(self):
        self.conversations: Dict[str, dict] = {}       # id -> dict
        self.conv_by_wa_id: Dict[str, str] = {}       # wa_id -> conv_id
        self.messages: List[dict] = []
        self.bookings: Dict[str, dict] = {}           # id -> dict
        self.booked_slots: set[Tuple[str, str]] = set() # (test_code, slot_time)
        self.idempotency_map: Dict[str, dict] = {}    # idempotency_key -> booking
        self.human_handoffs: List[dict] = []

    async def get_or_create_conversation(self, wa_id: str, patient_name: str = "Patient") -> dict:
        if wa_id in self.conv_by_wa_id:
            cid = self.conv_by_wa_id[wa_id]
            return self.conversations[cid]
        cid = str(uuid.uuid4())
        conv = {
            "id": cid,
            "wa_id": wa_id,
            "patient_name": patient_name,
            "status": "ACTIVE",
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cost_usd": 0.0,
            "total_cost_inr": 0.0,
            "created_at": datetime.now(timezone.utc)
        }
        self.conversations[cid] = conv
        self.conv_by_wa_id[wa_id] = cid
        return conv

    async def update_conversation_metrics(
        self,
        conversation_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_inr: float
    ):
        if conversation_id in self.conversations:
            c = self.conversations[conversation_id]
            c["total_prompt_tokens"] += prompt_tokens
            c["total_completion_tokens"] += completion_tokens
            c["total_cost_usd"] = round(c["total_cost_usd"] + cost_usd, 6)
            c["total_cost_inr"] = round(c["total_cost_inr"] + cost_inr, 4)

    async def update_conversation_status(self, conversation_id: str, status: str):
        if conversation_id in self.conversations:
            self.conversations[conversation_id]["status"] = status

    async def log_message(
        self,
        conversation_id: str,
        role: str,
        content: Optional[str] = None,
        wamid: Optional[str] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0
    ) -> dict:
        msg = {
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "wamid": wamid,
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "created_at": datetime.now(timezone.utc)
        }
        self.messages.append(msg)
        return msg

    async def get_recent_messages(self, conversation_id: str, limit: int = 6) -> List[dict]:
        conv_msgs = [m for m in self.messages if m["conversation_id"] == conversation_id]
        return conv_msgs[-limit:]

    async def create_booking(
        self,
        booking_reference: str,
        conversation_id: Optional[str],
        patient_phone: str,
        patient_name: str,
        test_code: str,
        slot_time: str,
        idempotency_key: str
    ) -> Tuple[bool, str, Optional[dict]]:
        # 1. Idempotency check: if key already exists, return previous booking
        if idempotency_key in self.idempotency_map:
            return True, "IDEMPOTENT_CONFIRMATION", self.idempotency_map[idempotency_key]

        # 2. Hard unique constraint simulation: slot already booked?
        slot_key = (test_code.upper(), slot_time)
        if slot_key in self.booked_slots:
            return False, "DOUBLE_BOOKING_PREVENTED", None

        # 3. Reserve slot
        bid = str(uuid.uuid4())
        booking = {
            "id": bid,
            "booking_reference": booking_reference,
            "conversation_id": conversation_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "test_code": test_code.upper(),
            "slot_time": slot_time,
            "status": "CONFIRMED",
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(timezone.utc)
        }
        self.bookings[bid] = booking
        self.booked_slots.add(slot_key)
        self.idempotency_map[idempotency_key] = booking
        return True, "CONFIRMED", booking

    async def create_human_handoff(
        self,
        conversation_id: str,
        wa_id: str,
        reason: str,
        priority: str = "MEDIUM"
    ) -> dict:
        record = {
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "wa_id": wa_id,
            "reason": reason,
            "priority": priority,
            "status": "PENDING",
            "created_at": datetime.now(timezone.utc)
        }
        self.human_handoffs.append(record)
        if conversation_id in self.conversations:
            self.conversations[conversation_id]["status"] = "HANDED_OFF"
        return record

    async def get_all_bookings(self) -> List[dict]:
        return list(self.bookings.values())

    async def get_conversation_by_wa_id(self, wa_id: str) -> Optional[dict]:
        cid = self.conv_by_wa_id.get(wa_id)
        return self.conversations.get(cid) if cid else None


class Database:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or settings.DATABASE_URL
        self._pool: Optional[asyncpg.Pool] = None
        self._mock_db: Optional[InMemoryDBMock] = None
        self._is_mock = False

    async def get_pool(self):
        if self._pool is not None:
            return self._pool
        if self._mock_db is not None:
            return self._mock_db

        try:
            pool = await asyncpg.create_pool(
                self.db_url,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.DB_POOL_MAX_SIZE,
                command_timeout=10.0
            )
            self._pool = pool
            self._is_mock = False
            logger.info("Connected to PostgreSQL pool successfully.")
            return pool
        except Exception as e:
            logger.warning(f"Could not connect to PostgreSQL at {self.db_url} ({e}). Using in-memory DB mock.")
            self._mock_db = InMemoryDBMock()
            self._is_mock = True
            return self._mock_db

    async def init_schema(self):
        """Initialize tables if running with PostgreSQL."""
        if self._is_mock or self._mock_db is not None:
            return
        pool = await self.get_pool()
        if self._is_mock:
            return

        schema_sql = """
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            wa_id VARCHAR(64) UNIQUE NOT NULL,
            patient_name VARCHAR(255) DEFAULT 'Patient',
            status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            total_prompt_tokens INT NOT NULL DEFAULT 0,
            total_completion_tokens INT NOT NULL DEFAULT 0,
            total_cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
            total_cost_inr NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS messages (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            wamid VARCHAR(128) UNIQUE,
            role VARCHAR(32) NOT NULL,
            content TEXT,
            tool_calls JSONB,
            tool_call_id VARCHAR(64),
            prompt_tokens INT DEFAULT 0,
            completion_tokens INT DEFAULT 0,
            cost_usd NUMERIC(10, 6) DEFAULT 0.000000,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS slots (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            test_code VARCHAR(64) NOT NULL,
            test_name VARCHAR(255) NOT NULL,
            slot_time TIMESTAMPTZ NOT NULL,
            max_capacity INT NOT NULL DEFAULT 1,
            current_booked INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_test_slot UNIQUE (test_code, slot_time)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            booking_reference VARCHAR(64) UNIQUE NOT NULL,
            conversation_id UUID REFERENCES conversations(id),
            patient_phone VARCHAR(64) NOT NULL,
            patient_name VARCHAR(255) NOT NULL,
            test_code VARCHAR(64) NOT NULL,
            slot_time TIMESTAMPTZ NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'CONFIRMED',
            idempotency_key VARCHAR(128) UNIQUE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_booking_slot UNIQUE (test_code, slot_time)
        );

        CREATE TABLE IF NOT EXISTS human_handoffs (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            wa_id VARCHAR(64) NOT NULL,
            reason TEXT NOT NULL,
            priority VARCHAR(16) NOT NULL DEFAULT 'MEDIUM',
            status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        async with pool.acquire() as conn:
            await conn.execute(schema_sql)

    async def get_or_create_conversation(self, wa_id: str, patient_name: str = "Patient") -> dict:
        target = await self.get_pool()
        if self._is_mock:
            return await target.get_or_create_conversation(wa_id, patient_name)

        async with target.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations (wa_id, patient_name)
                VALUES ($1, $2)
                ON CONFLICT (wa_id) DO UPDATE SET updated_at = NOW()
                RETURNING id, wa_id, patient_name, status, total_prompt_tokens, total_completion_tokens, total_cost_usd, total_cost_inr
                """,
                wa_id, patient_name
            )
            return dict(row)

    async def update_conversation_metrics(
        self,
        conversation_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_inr: float
    ):
        target = await self.get_pool()
        if self._is_mock:
            return await target.update_conversation_metrics(
                conversation_id, prompt_tokens, completion_tokens, cost_usd, cost_inr
            )

        async with target.acquire() as conn:
            await conn.execute(
                """
                UPDATE conversations
                SET total_prompt_tokens = total_prompt_tokens + $2,
                    total_completion_tokens = total_completion_tokens + $3,
                    total_cost_usd = total_cost_usd + $4,
                    total_cost_inr = total_cost_inr + $5,
                    updated_at = NOW()
                WHERE id = $1
                """,
                uuid.UUID(conversation_id), prompt_tokens, completion_tokens, cost_usd, cost_inr
            )

    async def update_conversation_status(self, conversation_id: str, status: str):
        target = await self.get_pool()
        if self._is_mock:
            return await target.update_conversation_status(conversation_id, status)

        async with target.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET status = $2, updated_at = NOW() WHERE id = $1",
                uuid.UUID(conversation_id), status
            )

    async def log_message(
        self,
        conversation_id: str,
        role: str,
        content: Optional[str] = None,
        wamid: Optional[str] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0
    ) -> dict:
        target = await self.get_pool()
        if self._is_mock:
            return await target.log_message(
                conversation_id, role, content, wamid, tool_calls, tool_call_id,
                prompt_tokens, completion_tokens, cost_usd
            )

        tool_calls_json = json.dumps(tool_calls, default=str) if tool_calls else None
        async with target.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO messages (conversation_id, wamid, role, content, tool_calls, tool_call_id, prompt_tokens, completion_tokens, cost_usd)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id, conversation_id, wamid, role, content, prompt_tokens, completion_tokens, cost_usd
                """,
                uuid.UUID(conversation_id), wamid, role, content, tool_calls_json, tool_call_id,
                prompt_tokens, completion_tokens, cost_usd
            )
            return dict(row)

    async def get_recent_messages(self, conversation_id: str, limit: int = 6) -> List[dict]:
        target = await self.get_pool()
        if self._is_mock:
            return await target.get_recent_messages(conversation_id, limit)

        async with target.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, tool_calls, tool_call_id
                FROM messages
                WHERE conversation_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                uuid.UUID(conversation_id), limit
            )
            result = []
            for r in reversed(rows):
                item = dict(r)
                if item.get("tool_calls"):
                    item["tool_calls"] = json.loads(item["tool_calls"])
                result.append(item)
            return result

    async def create_booking(
        self,
        booking_reference: str,
        conversation_id: Optional[str],
        patient_phone: str,
        patient_name: str,
        test_code: str,
        slot_time: str,
        idempotency_key: str
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        Attempt to atomically insert a booking into PostgreSQL.
        Enforces UNIQUE(test_code, slot_time) hard constraint.
        """
        target = await self.get_pool()
        if self._is_mock:
            return await target.create_booking(
                booking_reference, conversation_id, patient_phone,
                patient_name, test_code, slot_time, idempotency_key
            )

        cid = uuid.UUID(conversation_id) if conversation_id else None
        if isinstance(slot_time, str):
            clean_str = slot_time.strip().replace("Z", "+00:00")
            slot_dt = datetime.fromisoformat(clean_str)
        else:
            slot_dt = slot_time

        async with target.acquire() as conn:
            # 1. Check idempotency
            existing = await conn.fetchrow(
                "SELECT * FROM bookings WHERE idempotency_key = $1",
                idempotency_key
            )
            if existing:
                return True, "IDEMPOTENT_CONFIRMATION", dict(existing)

            # 2. Insert with constraint protection
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO bookings (booking_reference, conversation_id, patient_phone, patient_name, test_code, slot_time, idempotency_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id, booking_reference, patient_phone, patient_name, test_code, slot_time, status, idempotency_key
                    """,
                    booking_reference, cid, patient_phone, patient_name, test_code.upper(), slot_dt, idempotency_key
                )
                return True, "CONFIRMED", dict(row)
            except asyncpg.UniqueViolationError:
                return False, "DOUBLE_BOOKING_PREVENTED", None

    async def create_human_handoff(
        self,
        conversation_id: str,
        wa_id: str,
        reason: str,
        priority: str = "MEDIUM"
    ) -> dict:
        target = await self.get_pool()
        if self._is_mock:
            return await target.create_human_handoff(conversation_id, wa_id, reason, priority)

        async with target.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO human_handoffs (conversation_id, wa_id, reason, priority)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, conversation_id, wa_id, reason, priority, status
                    """,
                    uuid.UUID(conversation_id), wa_id, reason, priority
                )
                await conn.execute(
                    "UPDATE conversations SET status = 'HANDED_OFF', updated_at = NOW() WHERE id = $1",
                    uuid.UUID(conversation_id)
                )
                return dict(row)

    async def get_conversation_by_wa_id(self, wa_id: str) -> Optional[dict]:
        target = await self.get_pool()
        if self._is_mock:
            return await target.get_conversation_by_wa_id(wa_id)

        async with target.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversations WHERE wa_id = $1",
                wa_id
            )
            return dict(row) if row else None

    async def get_cluster_aggregates(self) -> Dict[str, Any]:
        """Query persistent database aggregates for global metrics reporting."""
        target = await self.get_pool()
        if self._is_mock:
            prompt = sum(c["total_prompt_tokens"] for c in self.conversations.values())
            compl = sum(c["total_completion_tokens"] for c in self.conversations.values())
            cost_u = sum(c["total_cost_usd"] for c in self.conversations.values())
            cost_i = sum(c["total_cost_inr"] for c in self.conversations.values())
            confirmed_b = sum(1 for b in self.bookings.values() if b.get("status") == "CONFIRMED")
            handoffs = len(self.human_handoffs)
            return {
                "prompt_tokens": prompt,
                "completion_tokens": compl,
                "total_tokens": prompt + compl,
                "cost_usd": round(cost_u, 6),
                "cost_inr": round(cost_i, 4),
                "confirmed_bookings": confirmed_b,
                "human_handoffs": handoffs
            }

        async with target.acquire() as conn:
            row_tokens = await conn.fetchrow(
                """
                SELECT 
                    COALESCE(SUM(total_prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(total_completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(total_cost_usd), 0.0) as cost_usd,
                    COALESCE(SUM(total_cost_inr), 0.0) as cost_inr
                FROM conversations
                """
            )
            row_b = await conn.fetchval("SELECT COUNT(*) FROM bookings WHERE status = 'CONFIRMED'")
            row_h = await conn.fetchval("SELECT COUNT(*) FROM human_handoffs")
            pt = int(row_tokens["prompt_tokens"])
            ct = int(row_tokens["completion_tokens"])
            return {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
                "cost_usd": round(float(row_tokens["cost_usd"]), 6),
                "cost_inr": round(float(row_tokens["cost_inr"]), 4),
                "confirmed_bookings": int(row_b or 0),
                "human_handoffs": int(row_h or 0)
            }

    async def close(self):
        if self._pool is not None and not self._is_mock:
            await self._pool.close()
        if self._pool is not None and not self._is_mock:
            await self._pool.close()


db = Database()
