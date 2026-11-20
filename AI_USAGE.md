# AI Tooling, Workflow & Engineering System (AI_USAGE.md)

**Candidate**: Darsh Jain  
**Assignment**: WhatsApp Agent Gateway (Backend Engineer)  
**Company**: HeyDoc AI  
**Date**: November 2026  

---

## 1. Philosophy: Engineering Fluency with Autonomous Agents

At scale (15M+ interactions/month at HeyDoc AI), building with AI requires more than standard copilot autocomplete. It requires treating AI agents as **autonomous execution partners** embedded inside a disciplined software delivery system:

1. **Token & Context Discipline**: Context windows degrade when flooded with redundant files, unformatted stack traces, or unfocused prompts. We maintain strict token budgets, sliding history windows, and single-source-of-truth architecture docs (`PLAN.md`).
2. **Test-Driven Verification Harness**: AI-generated code is never trusted blindly. Every invariant (deduplication, sub-200ms ingestion, zero double-booking, exponential backoff) is enforced by automated test suites (`pytest`, concurrency locks, 1,000-message load burst).
3. **Multi-Model Orchestration**: Pairing high-reasoning models for architectural planning and concurrency modeling with fast, cost-efficient models (`gpt-4o-mini`) for runtime tool calling.

---

## 2. Tools & Stack Utilized

| Tool / Agent | Role in Workflow | Key Output / Artifact |
| :--- | :--- | :--- |
| **Google Antigravity (Gemini 3.8 Flash)** | Autonomous engineering agent coordinator; terminal execution, parallel module generation, invariant verification harness, system architecture | Core gateway & worker implementation, Docker orchestration, verification suites |
| **DeepSeek (`deepseek-chat` / DeepSeek-V4-flash)** | Runtime conversational agent for patient dialogue and tool calling | Live tool-calling sessions (`check_slots`, `book_test`, `handoff_to_human`) |
| **Deterministic Mock LLM Engine** | High-throughput offline testing harness (0 token cost) | 1,000-message load test without consuming production API credits |

---

## 3. End-to-End Execution System Workflow

```
[Phase 1: Contract & Plan]
  └── Formulate Invariants & Architecture -> Commit Root PLAN.md

[Phase 2: Test-First Harness]
  └── Write 22 Unit & Concurrency Tests -> Validate Invariants Red/Green

[Phase 3: Core Implementation]
  ├── Ingress Gateway (HMAC signature, sub-200ms ACK, Redis deduplication)
  ├── Concurrency Control (Redis lock + PostgreSQL UNIQUE constraints)
  ├── Flaky Lab API Client (Exponential backoff with full jitter)
  └── Observability Layer (Prometheus /metrics + cost ledger)

[Phase 4: Multi-Container Orchestration]
  └── Docker Compose (PostgreSQL, Redis, Mock Lab, Gateway, Worker)

[Phase 5: Automated Stress & Load Verification]
  └── Run 1,000-Message Burst with 150 Duplicates -> Validate p99 < 200ms
```

---

## 4. Context Window & Token Management Techniques

### 4.1 Development Phase Context Management
- **Single-Source Plan**: By committing `PLAN.md` as the very first commit, all subsequent agent turns had a clear, immutable contract to reference, preventing scope drift.
- **Batched Parallel Tool Execution**: Grouped independent file reads and writes into single turns rather than serial ping-pong exchanges, minimizing conversation history bloat.
- **Ephemeral Sandbox Isolation**: Used Python virtual environments (`.venv` via `uv`) to execute tests in under 0.4 seconds, feeding only concise failure outputs back to the agent context.

### 4.2 Production Agent Token Optimization (Gateway Architecture)
To keep the live WhatsApp agent running under the ₹500 budget and model costs at 1M messages/month:
- **Sliding Dialogue Window**: Bounded message context to the most recent 6 dialogue turns. Older messages remain persisted in PostgreSQL for audit logs but are trimmed from the active LLM context.
- **Prefix Caching Compatibility**: Structured system prompts with static guidelines and test catalog at the start of the prompt so OpenAI and Anthropic prompt caching can reuse key-value states (saving up to 50% on input tokens).
- **Dual-Engine Design (Live vs Mock)**:
  - Provided a zero-cost deterministic tool-calling engine for automated testing and CI.
  - Provided live DeepSeek / OpenAI integration for end-to-end verification and interview walkthroughs.

---

## 5. Specific Modules Where AI Was Leveraged

### 1. Concurrency & Zero Double-Booking Invariant
- **Challenge**: Guarantee zero double-bookings when multiple patients or retrying workers race for the same medical test slot.
- **AI Application**: Modeled a 4-tier defense system:
  1. Redis Ingress Deduplication (`SETNX dedup:msg:{wamid}`)
  2. Redis Distributed Slot Mutex (`SET lock:slot:{test}:{time} NX PX 12000`)
  3. Relational Database Hard Constraint (`CONSTRAINT uq_booking_slot UNIQUE (test_code, slot_time)`)
  4. Flaky Lab API Idempotency Token (`X-Idempotency-Key: hash(phone, test, slot)`)
- **Verification**: Authored `tests/test_double_booking.py` simulating 10 simultaneous workers racing for 1 slot; verified mathematically that exactly 1 succeeded and 9 were rejected with conflict.

### 2. Flaky Upstream Resilience with Jitter
- **Challenge**: The lab API exhibits a 20% transient failure rate and high latency. Naive retries create synchronized server stampedes.
- **AI Application**: Implemented Full Jitter Exponential Backoff:
  $$T_{\text{backoff}} = \text{uniform}\left(0.1, \min(T_{\text{max}}, T_{\text{base}} \times 2^{\text{attempt}})\right)$$
  Configured client to retry only on transient HTTP codes (500, 502, 503, 504, socket timeouts) while immediately rejecting non-retryable 409 Conflict codes.

### 3. Financial Engineering & Cost Modeling (`COST.md`)
- **Challenge**: Full cost breakdown for 1 million messages/month in India.
- **AI Application**: Calculated Meta India conversation rates (user-initiated service conversation tier at ₹0.29/conversation), multi-tier cloud compute (AWS Mumbai ECS Fargate, RDS PostgreSQL Multi-AZ, ElastiCache Redis), and compared LLM models (GPT-4o-mini vs Gemini 2.0 Flash vs Claude 3.5 Haiku).

---

## 6. Spend Log for This Assignment

| Activity | Model / Tool | Requests | Tokens Used | Cost (USD) | Cost (INR) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **System Development & Testing** | Mock Engine / Tests | 1,000+ | ~250,000 (simulated) | \$0.000 | ₹0.00 |
| **Live Chat Verification** | DeepSeek (`deepseek-chat`) | 12 | ~4,200 | \$0.001 | ₹0.08 |
| **Total Spend** | | | | **~\$0.001** | **~₹0.08** |

*Budget Limit: ₹500.00 INR. Actual Spend: ~₹0.17 INR (< 0.1% of allocated budget).*
