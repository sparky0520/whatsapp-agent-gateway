# Architectural Plan: WhatsApp Agent Gateway for Diagnostics Lab

**Project**: WhatsApp Agent Gateway (Backend Engineering Assignment)  
**Author**: Darsh Jain  
**Repository**: `whatsapp-agent-gateway`  
**Date**: November 2026  

---

## 1. Problem Statement & Mission-Critical Requirements

A diagnostic lab network receives high volumes of patient test booking requests over WhatsApp. Healthcare booking workflows face distinct technical challenges compared to standard e-commerce systems:

1. **Unpredictable Webhook Bursts & Strict Deadlines**:
   - Meta WhatsApp Cloud API webhooks arrive in sudden bursts (e.g. morning peak hours).
   - Meta mandates an HTTP `200 OK` acknowledgment within **200 ms**. Failure to acknowledge triggers Meta's exponential retry mechanism, creating cascading server stampedes and redundant work.
2. **At-Least-Once Delivery & Duplicate Messages**:
   - Network partitions, mobile client retries, and Meta webhook retransmissions cause webhooks to arrive twice or more with the same Message ID (`wamid`).
3. **Flaky & High-Latency Lab Booking API**:
   - The lab's legacy booking API exhibits simulated high latency (500 ms – 2,000 ms) and transient failures (~20% HTTP 500/503 errors and socket timeouts).
4. **Zero Double-Booking Invariant**:
   - Diagnostic appointments involve limited phlebotomist capacity, equipment availability, and medical time slots.
   - **Double-booking is strictly prohibited** under any concurrent scenario, network retry, or worker race condition.
5. **Observability, Token & Cost Tracking**:
   - Healthcare conversational agents incur LLM token costs. Every interaction must be tracked at granular per-conversation level and exported via `/metrics` (Prometheus format) for real-time cost visibility and alerting.
6. **Rate Limiting & Abuse Prevention**:
   - Malicious or erratic actors must be throttled per sender phone number to protect backend resources and LLM spend.

---

## 2. Users & Personas

| Persona | Channel | Needs & Constraints |
| :--- | :--- | :--- |
| **Patient / Customer** | WhatsApp Chat | Wants fast, frictionless appointment booking, availability checks, clear rescheduling/cancellation, and immediate confirmation without technical hiccups. |
| **Lab Phlebotomist / Staff** | Internal Dashboard / SMS | Relies on perfectly non-conflicting schedules. Cannot accommodate overlapping patient slots. |
| **Human Medical Agent** | Escalation Queue | Handles edge cases (complex medical prep questions, emergencies, booking conflicts) triggered by `handoff_to_human`. |
| **Platform SRE / Ops** | `/metrics` & Logs | Monitors webhook p99 latencies, queue depths, worker throughput, LLM token burn, retry rates, and double-booking prevention events. |

---

## 3. Architecture & Data Flow

### 3.1 Architectural Sketch

```
                      +-----------------------------+
                      |   WhatsApp Cloud API (Meta) |
                      +--------------+--------------+
                                     |
               1. POST /webhook      | HMAC-SHA256 Sig Check
               (Burst traffic)       v
         +-------------------------------------------------------+
         |               API GATEWAY (FastAPI)                   |
         |  - Verify X-Hub-Signature-256                         |
         |  - Phone Rate Limiting (Redis Sliding Window)         |
         |  - Message Deduplication Check (Redis SET NX EX)      |
         |  - Push Job to Redis Queue (LPUSH / Streams)          |
         |  - Response: HTTP 200 OK (< 200ms)                    |
         +---------------------------+---------------------------+
                                     |
                                     | 2. Enqueue Message Job
                                     v
                       +---------------------------+
                       |    Redis Queue / Broker   |
                       |  - Key: "queue:whatsapp"  |
                       +-------------+-------------+
                                     |
                                     | 3. Worker Pop / Dequeue
                                     v
         +-------------------------------------------------------+
         |               ASYNCHRONOUS WORKER                     |
         |                                                       |
         |  - Fetch / Init Conversation State in PostgreSQL      |
         |  - Assemble Prompt History with Context Trimming      |
         |  - Call LLM (Tool Calling: check_slots, book_test,    |
         |               handoff_to_human)                       |
         |  - Record Token Usage & Estimated Cost in PostgreSQL  |
         |  - Update Prometheus Metrics                          |
         +-------------+-----------------------------+-----------+
                       |                             |
         4. Tool Calls |                             | 5. DB Persist
                       v                             v
        +----------------------------+  +------------------------+
        |     MOCK LAB API           |  |      POSTGRESQL        |
        |  - 20% Injected Failures   |  |  - conversations       |
        |  - 500ms-2000ms Latency    |  |  - messages            |
        |  - Exponential Backoff     |  |  - bookings (UNIQUE)   |
        |    + Jitter Retry Client   |  |  - slots (Inventory)   |
        |  - Idempotency Key Check   |  |  - human_handoffs      |
        +----------------------------+  +------------------------+
```

### 3.2 Sequence of Operations

1. **Ingress (< 200ms)**:
   - Meta POSTs payload to `/webhook`.
   - Gateway verifies `X-Hub-Signature-256` using the configured `WHATSAPP_APP_SECRET`.
   - Rate limiter checks if sender phone exceeds rate limit (e.g. 10 msgs/min) using Redis sliding window.
   - Message Deduplicator runs atomic `SETNX dedup:msg:{wamid} "1" EX 86400`:
     - If `0`: Duplicate message! Gateway immediately returns `HTTP 200 OK` (so Meta doesn't retry) but discards duplicate processing.
     - If `1`: New message! Enqueues job `{ "wamid": ..., "from": ..., "text": ..., "timestamp": ... }` into Redis.
   - Gateway returns `HTTP 200 OK` with response latency consistently under **50 ms**.
2. **Worker Processing**:
   - Worker consumes payload from Redis.
   - Loads conversation context from PostgreSQL (`conversations` and recent `messages`).
   - Invokes LLM with current history and system prompt defining lab scope and tool definitions.
   - LLM decides tool call or text response.
   - When calling `check_slots`: queries Mock Lab API.
   - When calling `book_test`:
     - Generates deterministic/UUID `idempotency_key` based on `(phone, test_type, slot_time)`.
     - Acquires distributed lock `lock:slot:{test_type}:{slot_time}`.
     - Executes booking against Mock Lab API with exponential backoff & jitter (max 3 retries).
     - Inserts booking into PostgreSQL with `UNIQUE(test_type, slot_time, status='CONFIRMED')` constraint.
     - Releases lock.
   - When calling `handoff_to_human`:
     - Sets conversation status to `HANDED_OFF`.
     - Creates record in `human_handoffs` table.
   - Calculates token burn (prompt + completion) and incremental cost; writes to PostgreSQL and increments Prometheus counters.
   - Sends simulated or live WhatsApp reply back to patient.

---

## 4. Concurrency, Invariants & Zero Double-Booking Strategy

### 4.1 The Double-Booking Danger Vectors
1. **Parallel Webhooks**: Two messages from the same user or different users arrive simultaneously requesting the same time slot (e.g., `2026-11-21 10:00 AM` for `Lipid Profile`).
2. **Worker Retries**: Flaky Lab API fails with network timeout after creating the slot on their side, worker retries and risks booking a second slot or creating duplicate records.
3. **Stale LLM Context**: LLM sees slot available from previous turn, but another patient booked it in the interim.

### 4.2 Multi-Layer Defense in Depth
To guarantee **mathematically zero double-booking**:

| Layer | Mechanism | Implementation |
| :--- | :--- | :--- |
| **Layer 1: Webhook Dedup** | Ingress message deduplication | Redis atomic `SETNX dedup:msg:{wamid} 1 EX 86400`. Retried duplicate webhooks are dropped before worker dispatch. |
| **Layer 2: Slot Lock** | Distributed concurrency mutex | Redis lock `SET lock:slot:{test_type}:{slot_time} {worker_id} NX PX 10000` acquired before checking/reserving a slot. |
| **Layer 3: DB Unique Constraint** | Hard relational database constraint | PostgreSQL table `bookings` enforces `UNIQUE (test_code, slot_time)` where `status = 'CONFIRMED'`. Any concurrent attempt triggers an `IntegrityError` / unique constraint violation. |
| **Layer 4: Lab API Idempotency** | Idempotency tokens on upstream calls | Every `book_test` call sends `X-Idempotency-Key: hash(phone, test, slot)`. If Mock Lab API receives retried call, it returns the existing booking reference without re-allocating. |
| **Layer 5: Flaky Retries with Backoff** | Resilient HTTP client | Retries on HTTP 500, 502, 503, 504 and connection timeouts with formula: $T = \min(T_{max}, T_{base} \times 2^{attempt}) \pm \text{jitter}$. Non-retryable errors (e.g., 409 Conflict / Slot Taken) immediately surface as "slot unavailable" to the LLM. |

---

## 5. Data Model (PostgreSQL & Redis)

### 5.1 PostgreSQL Relational Schema

```sql
-- Conversations Table
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wa_id VARCHAR(64) UNIQUE NOT NULL,       -- Patient WhatsApp Phone Number
    patient_name VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, HANDED_OFF, COMPLETED
    total_prompt_tokens INT NOT NULL DEFAULT 0,
    total_completion_tokens INT NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    total_cost_inr NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Messages Table (Audit Log & LLM Context)
CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    wamid VARCHAR(128) UNIQUE,               -- WhatsApp Message ID (nullable for system/assistant)
    role VARCHAR(32) NOT NULL,               -- user, assistant, system, tool
    content TEXT,
    tool_calls JSONB,
    tool_call_id VARCHAR(64),
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    cost_usd NUMERIC(10, 6) DEFAULT 0.000000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation_created ON messages(conversation_id, created_at ASC);

-- Available Slots (Mock Lab Inventory)
CREATE TABLE slots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_code VARCHAR(64) NOT NULL,          -- e.g. 'CBC', 'LIPID', 'FBS'
    slot_time TIMESTAMPTZ NOT NULL,
    max_capacity INT NOT NULL DEFAULT 1,
    current_booked INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_test_slot UNIQUE (test_code, slot_time)
);

-- Bookings Table (Zero Double-Booking Enforcement)
CREATE TABLE bookings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_reference VARCHAR(64) UNIQUE NOT NULL, -- e.g. 'BK-98421'
    conversation_id UUID REFERENCES conversations(id),
    patient_phone VARCHAR(64) NOT NULL,
    patient_name VARCHAR(255) NOT NULL,
    test_code VARCHAR(64) NOT NULL,
    slot_time TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'CONFIRMED', -- CONFIRMED, CANCELLED
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    lab_api_attempts INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Hard guarantee: No two active bookings for the same test and slot
    CONSTRAINT uq_confirmed_slot UNIQUE (test_code, slot_time)
);

CREATE INDEX idx_bookings_phone ON bookings(patient_phone);

-- Human Handoff Records
CREATE TABLE human_handoffs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    wa_id VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    priority VARCHAR(16) NOT NULL DEFAULT 'MEDIUM', -- LOW, MEDIUM, HIGH, URGENT
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',  -- PENDING, RESOLVED
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 5.2 Redis Key Schema

| Key Pattern | Type | TTL | Purpose |
| :--- | :--- | :--- | :--- |
| `dedup:msg:{wamid}` | String | 24 Hours (86400s) | Ingress deduplication; drops repeat webhook deliveries. |
| `ratelimit:{wa_id}` | Sorted Set / Counter | 60s | Sliding window rate limit per phone number (e.g. 10 req/min). |
| `queue:whatsapp` | List / Stream | N/A | Worker message ingestion queue. |
| `lock:slot:{test}:{time}` | String | 15s | Distributed mutex preventing race conditions during slot reservations. |

---

## 6. API Contracts & Payload Formats

### 6.1 WhatsApp Webhook Ingress (Gateway)

#### Webhook Verification (`GET /webhook`)
- **Query Parameters**:
  - `hub.mode`: `"subscribe"`
  - `hub.challenge`: `"<random_string>"`
  - `hub.verify_token`: configured `WHATSAPP_VERIFY_TOKEN`
- **Response**: `200 OK` with body `<hub.challenge>` on match; `403 Forbidden` on mismatch.

#### Webhook Notification (`POST /webhook`)
- **Headers**:
  - `X-Hub-Signature-256`: `sha256=<hex_hmac_sha256_of_body>`
  - `Content-Type`: `application/json`
- **Standard WhatsApp Cloud API Payload Structure**:
```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "15550012345",
              "phone_number_id": "PHONE_NUMBER_ID"
            },
            "contacts": [
              {
                "profile": { "name": "Aarav Sharma" },
                "wa_id": "919876543210"
              }
            ],
            "messages": [
              {
                "from": "919876543210",
                "id": "wamid.HBgMOTExOTg3NjU0MzIxMBUCABIYFDNBMTA2OTI5Mzk2NUI4QkVDOTUzAA==",
                "timestamp": "1732085400",
                "text": { "body": "Hi, I need to book a Complete Blood Count test for tomorrow morning" },
                "type": "text"
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}
```
- **Gateway Response**: `HTTP 200 OK` with JSON `{"status": "queued", "wamid": "..."}` within < 200 ms.

---

### 6.2 Mock Lab API Contract

The mock lab API simulates the diagnostic lab's backend service.

#### 1. Check Slots (`GET /lab/slots`)
- **Query Params**:
  - `test_code`: e.g. `"CBC"`, `"LIPID"`, `"FBS"`, `"COVID"`
  - `date`: ISO Date format (`YYYY-MM-DD`)
- **Response**:
```json
{
  "test_code": "CBC",
  "test_name": "Complete Blood Count",
  "date": "2026-11-21",
  "available_slots": [
    { "slot_time": "2026-11-21T08:00:00Z", "available": true },
    { "slot_time": "2026-11-21T09:00:00Z", "available": false },
    { "slot_time": "2026-11-21T10:00:00Z", "available": true }
  ]
}
```

#### 2. Book Test (`POST /lab/book`)
- **Headers**:
  - `X-Idempotency-Key`: UUID string
- **Request Body**:
```json
{
  "patient_name": "Aarav Sharma",
  "patient_phone": "919876543210",
  "test_code": "CBC",
  "slot_time": "2026-11-21T10:00:00Z",
  "idempotency_key": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
}
```
- **Responses**:
  - `200 OK`: `{"status": "CONFIRMED", "booking_reference": "BK-54210", "slot_time": "...", "patient_name": "..."}`
  - `409 Conflict`: `{"status": "FAILED", "error": "SLOT_ALREADY_BOOKED", "message": "This slot has already been reserved."}`
  - `500 / 503 Internal Error`: Injected 20% random failure to test worker retries.

---

### 6.3 Observability & Metrics Endpoint (`GET /metrics`)

Exposes metrics in standard Prometheus text format:
```
# HELP whatsapp_webhook_requests_total Total number of WhatsApp webhook requests received
# TYPE whatsapp_webhook_requests_total counter
whatsapp_webhook_requests_total{status="accepted"} 940
whatsapp_webhook_requests_total{status="duplicate"} 60
whatsapp_webhook_requests_total{status="rate_limited"} 0
whatsapp_webhook_requests_total{status="invalid_signature"} 0

# HELP whatsapp_webhook_duration_seconds Webhook ingress acknowledgment latency
# TYPE whatsapp_webhook_duration_seconds histogram
whatsapp_webhook_duration_seconds_bucket{le="0.05"} 980
whatsapp_webhook_duration_seconds_bucket{le="0.1"} 1000
whatsapp_webhook_duration_seconds_bucket{le="0.2"} 1000

# HELP lab_api_requests_total Total requests made to lab API
# TYPE lab_api_requests_total counter
lab_api_requests_total{endpoint="book",status="success"} 420
lab_api_requests_total{endpoint="book",status="failure"} 84
lab_api_retries_total 84

# HELP bookings_total Total booking outcomes
# TYPE bookings_total counter
bookings_total{status="confirmed"} 420
bookings_total{status="double_booking_prevented"} 15

# HELP llm_tokens_total Total LLM tokens consumed
# TYPE llm_tokens_total counter
llm_tokens_total{type="prompt"} 142500
llm_tokens_total{type="completion"} 28500

# HELP llm_cost_total Cumulative estimated LLM cost
# TYPE llm_cost_total counter
llm_cost_total{currency="USD"} 0.041250
llm_cost_total{currency="INR"} 3.506250
```

---

## 7. Agent & Tool Calling Architecture

### 7.1 LLM Tools Specification

```json
[
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
            "description": "Date in YYYY-MM-DD format"
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
      "description": "Book a diagnostic test slot for the patient. Must only be called after verifying slot availability.",
      "parameters": {
        "type": "object",
        "properties": {
          "patient_name": { "type": "string", "description": "Full name of the patient" },
          "patient_phone": { "type": "string", "description": "Patient WhatsApp phone number" },
          "test_code": { "type": "string", "enum": ["CBC", "LIPID", "FBS", "THYROID", "COVID"] },
          "slot_time": { "type": "string", "description": "ISO-8601 formatted datetime slot, e.g. 2026-11-21T09:00:00Z" }
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
          "reason": { "type": "string", "description": "Detailed explanation for escalation" },
          "priority": { "type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"] }
        },
        "required": ["reason", "priority"]
      }
    }
  }
]
```

### 7.2 Context Window Optimization & Token Discipline
- **Sliding History Window**: Maintain only the last 6 dialogue turns (user + assistant + tool calls) in the active LLM context.
- **System Prompt Caching**: Keep system instructions concise (< 450 tokens) and cache-friendly.
- **Cost Calculation Model**:
  - Uses OpenAI / Gemini pricing tiers (e.g. GPT-4o-mini: \$0.150 per 1M input tokens, \$0.600 per 1M output tokens; USD/INR conversion rate: 1 USD = 85 INR).
  - Also includes a built-in deterministic Mock LLM Engine for high-volume automated testing without depleting real API credits or requiring external credentials.

---

## 8. Cost Estimate for 1,000,000 Messages / Month (Preview)

A full operational analysis is provided in `COST.md`. Below is the executive cost preview at 1M messages/month:

| Component | Sizing & Basis | Monthly Cost (USD) | Monthly Cost (INR) |
| :--- | :--- | :--- | :--- |
| **Compute (Gateway + Worker)** | 3x ECS / K8s containers (2 vCPU, 4GB RAM) | \$72.00 | ₹6,120 |
| **Managed PostgreSQL** | AWS RDS db.t4g.medium (2 vCPU, 4GB, 50GB SSD) | \$68.00 | ₹5,780 |
| **Managed Redis** | AWS ElastiCache t4g.small (1.37GB RAM) | \$24.00 | ₹2,040 |
| **Network & Egress** | Data transfer & Application Load Balancer | \$22.00 | ₹1,870 |
| **WhatsApp Cloud API Fees** | ~200k user-initiated service conversations (free tier + paid) | \$0.00 – \$120.00 | ₹0 – ₹10,200 |
| **LLM Inference (GPT-4o-mini)**| ~250k LLM calls (avg 500 in, 100 out tokens) | \$33.75 | ₹2,868 |
| **Total Estimated Run Cost** | **Comprehensive Full-Stack Cost** | **~\$220 – \$340** | **~₹18,700 – ₹28,900** |

*Key takeaway: LLM token costs account for only ~15% of the total operating budget when using a cost-efficient model with sliding context windows, while infrastructure reliability and database durability dominate the architecture.*

---

## 9. Implementation Roadmap & Milestones

1. **Commit 1**: `PLAN.md` (this architectural specification and contract).
2. **Commit 2**: Core application architecture:
   - FastAPI Ingress Gateway with signature verification, rate limiter, and Redis deduplication.
   - Mock Lab API service with 20% failure simulation and latency injection.
   - Asynchronous worker with resilient exponential backoff client.
   - PostgreSQL storage layer with `UNIQUE` constraint guarantees against double booking.
   - LLM tool calling agent with `check_slots`, `book_test`, `handoff_to_human`.
   - Per-conversation token & cost ledger with Prometheus `/metrics` exporter.
3. **Commit 3**: Containerization & Docker Compose orchestration:
   - Multi-container `docker-compose.yml` (`gateway`, `worker`, `mock-lab`, `postgres`, `redis`).
   - Health checks, network isolation, and environment configuration.
4. **Commit 4**: Comprehensive test suite & load testing script:
   - Unit and integration tests for deduplication, signature checking, retries, and double-booking prevention.
   - 1,000-message burst load test script injecting duplicates.
   - Automated report generation.
5. **Commit 5**: Documentation deliverables:
   - `COST.md` (comprehensive 1M messages/month analysis).
   - `AI_USAGE.md` (AI agent workflow, token efficiency, Claude Code / Antigravity methodology).
   - `README.md` (run steps, architecture guide, benchmark results, actual spend log).
