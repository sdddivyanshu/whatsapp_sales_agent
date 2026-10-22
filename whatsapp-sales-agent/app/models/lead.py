"""Lead models and the workflow state enum."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LeadState(str, Enum):
    NEW_LEAD = "NEW_LEAD"
    VALIDATING = "VALIDATING"
    INVALID_LEAD = "INVALID_LEAD"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    DUPLICATE_REVIEW = "DUPLICATE_REVIEW"
    SKIPPED_EXISTING = "SKIPPED_EXISTING"
    READY_TO_CONTACT = "READY_TO_CONTACT"
    MESSAGE_SENT = "MESSAGE_SENT"
    DELIVERED = "DELIVERED"
    MESSAGE_FAILED = "MESSAGE_FAILED"
    CONVERSATION_ACTIVE = "CONVERSATION_ACTIVE"
    QUALIFYING = "QUALIFYING"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    HUMAN_HANDOVER = "HUMAN_HANDOVER"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    SUMMARY_PENDING = "SUMMARY_PENDING"
    SHEET_SYNC_PENDING = "SHEET_SYNC_PENDING"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


REQUIRED_COLUMNS = ["customer_name", "company_name", "mobile_number", "requirement"]
OPTIONAL_COLUMNS = [
    "lead_id", "email", "city", "state", "industry",
    "preferred_language", "consent_status", "source",
]
INPUT_COLUMNS = ["lead_id", *REQUIRED_COLUMNS, *OPTIONAL_COLUMNS[1:]]

OUTPUT_COLUMNS = [
    "agent_status",
    "first_contact_at",
    "last_message_at",
    "conversation_status",
    "conversation_summary",
    "qualification_status",
    "qualification_reason",
    "product_interest",
    "quoted_price",
    "price_source",
    "purchase_timeline",
    "next_action",
    "follow_up_required",
    "assigned_to",
    "closed_at",
    "last_error",
]


class LeadInput(BaseModel):
    """A lead row as read from the source (all values are raw strings)."""

    lead_id: str = ""
    customer_name: str = ""
    company_name: str = ""
    mobile_number: str = ""
    requirement: str = ""
    email: str = ""
    city: str = ""
    state: str = ""
    industry: str = ""
    preferred_language: str = ""
    consent_status: str = ""
    source: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class ValidationOutcome(BaseModel):
    valid: bool
    mobile_e164: str | None = None
    state: LeadState
    reasons: list[str] = Field(default_factory=list)
