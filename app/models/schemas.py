"""
Pydantic models and schemas for WhatsApp Cloud API payloads,
Mock Lab API contracts, and internal agent state representation.
"""
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field


# ============================================================================
# WhatsApp Cloud API Webhook Schemas
# ============================================================================

class TextContent(BaseModel):
    body: str


class WhatsAppMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str = "text"
    text: Optional[TextContent] = None

    model_config = {
        "populate_by_name": True
    }


class ContactProfile(BaseModel):
    name: Optional[str] = "Patient"


class WhatsAppContact(BaseModel):
    profile: Optional[ContactProfile] = None
    wa_id: str


class ValueMetadata(BaseModel):
    display_phone_number: Optional[str] = None
    phone_number_id: Optional[str] = None


class ChangeValue(BaseModel):
    messaging_product: str = "whatsapp"
    metadata: Optional[ValueMetadata] = None
    contacts: Optional[List[WhatsAppContact]] = None
    messages: Optional[List[WhatsAppMessage]] = None


class WebhookChange(BaseModel):
    value: ChangeValue
    field: str = "messages"


class WebhookEntry(BaseModel):
    id: str
    changes: List[WebhookChange]


class WhatsAppWebhookPayload(BaseModel):
    object: str = "whatsapp_business_account"
    entry: List[WebhookEntry]


# ============================================================================
# Internal Ingestion DTO
# ============================================================================

class IngestedMessageJob(BaseModel):
    wamid: str
    wa_id: str
    sender_name: str
    text: str
    timestamp: str
    received_at_epoch: float


# ============================================================================
# Mock Lab API Contracts
# ============================================================================

class SlotItem(BaseModel):
    slot_time: str
    available: bool


class CheckSlotsResponse(BaseModel):
    test_code: str
    test_name: str
    date: str
    available_slots: List[SlotItem]


class BookTestRequest(BaseModel):
    patient_name: str
    patient_phone: str
    test_code: str
    slot_time: str
    idempotency_key: str


class BookTestResponse(BaseModel):
    status: Literal["CONFIRMED", "FAILED"]
    booking_reference: Optional[str] = None
    patient_name: str
    patient_phone: str
    test_code: str
    slot_time: str
    error: Optional[str] = None
    message: Optional[str] = None


# ============================================================================
# LLM Tool Parameter Schemas
# ============================================================================

class CheckSlotsArgs(BaseModel):
    test_code: str = Field(description="Diagnostic test code, e.g. CBC, LIPID, FBS, THYROID, COVID")
    date: str = Field(description="Target date in YYYY-MM-DD format")


class BookTestArgs(BaseModel):
    patient_name: str = Field(description="Full name of patient")
    patient_phone: str = Field(description="WhatsApp phone number of patient")
    test_code: str = Field(description="Test code, e.g. CBC, LIPID, FBS, THYROID, COVID")
    slot_time: str = Field(description="Selected ISO slot time, e.g. 2026-11-21T09:00:00Z")


class HandoffArgs(BaseModel):
    reason: str = Field(description="Reason for human coordinator escalation")
    priority: Literal["LOW", "MEDIUM", "HIGH", "URGENT"] = Field(
        default="MEDIUM",
        description="Escalation urgency level"
    )


# ============================================================================
# Conversation & Metrics DTOs
# ============================================================================

class ConversationSummary(BaseModel):
    wa_id: str
    patient_name: Optional[str]
    status: str
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    total_cost_inr: float
    message_count: int
    booking_count: int
    human_handoff_count: int
