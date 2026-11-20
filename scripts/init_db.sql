-- PostgreSQL Initialization Schema for WhatsApp Agent Gateway

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Conversations Table: Tracks session state, token burn, and total costs
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    wa_id VARCHAR(64) UNIQUE NOT NULL,
    patient_name VARCHAR(255) DEFAULT 'Patient',
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, HANDED_OFF, COMPLETED
    total_prompt_tokens INT NOT NULL DEFAULT 0,
    total_completion_tokens INT NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    total_cost_inr NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_wa_id ON conversations(wa_id);

-- 2. Messages Table: Full conversational context, role history, and token audit
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    wamid VARCHAR(128) UNIQUE,
    role VARCHAR(32) NOT NULL, -- user, assistant, system, tool
    content TEXT,
    tool_calls JSONB,
    tool_call_id VARCHAR(64),
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    cost_usd NUMERIC(10, 6) DEFAULT 0.000000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages(conversation_id, created_at ASC);

-- 3. Available Slots: Mock lab inventory
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

-- 4. Bookings Table: Hard Relational Guarantee Against Double-Booking
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
    -- Core Invariant: Absolutely no two confirmed bookings for the same test and slot time!
    CONSTRAINT uq_booking_slot UNIQUE (test_code, slot_time)
);

CREATE INDEX IF NOT EXISTS idx_bookings_phone ON bookings(patient_phone);
CREATE INDEX IF NOT EXISTS idx_bookings_idempotency ON bookings(idempotency_key);

-- 5. Human Handoffs Table
CREATE TABLE IF NOT EXISTS human_handoffs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    wa_id VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    priority VARCHAR(16) NOT NULL DEFAULT 'MEDIUM',
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed initial test inventory
INSERT INTO slots (test_code, test_name, slot_time, max_capacity, current_booked)
VALUES 
    ('CBC', 'Complete Blood Count', '2026-11-21 08:00:00+00', 1, 0),
    ('CBC', 'Complete Blood Count', '2026-11-21 09:00:00+00', 1, 0),
    ('CBC', 'Complete Blood Count', '2026-11-21 10:00:00+00', 1, 0),
    ('CBC', 'Complete Blood Count', '2026-11-21 11:00:00+00', 1, 0),
    ('CBC', 'Complete Blood Count', '2026-11-21 14:00:00+00', 1, 0),
    ('LIPID', 'Lipid Profile', '2026-11-21 08:00:00+00', 1, 0),
    ('LIPID', 'Lipid Profile', '2026-11-21 09:00:00+00', 1, 0),
    ('LIPID', 'Lipid Profile', '2026-11-21 10:00:00+00', 1, 0),
    ('FBS', 'Fasting Blood Sugar', '2026-11-21 08:00:00+00', 1, 0),
    ('FBS', 'Fasting Blood Sugar', '2026-11-21 09:00:00+00', 1, 0),
    ('THYROID', 'Thyroid Profile', '2026-11-21 10:00:00+00', 1, 0),
    ('THYROID', 'Thyroid Profile', '2026-11-21 11:00:00+00', 1, 0),
    ('COVID', 'RT-PCR Covid Test', '2026-11-21 12:00:00+00', 1, 0)
ON CONFLICT (test_code, slot_time) DO NOTHING;
