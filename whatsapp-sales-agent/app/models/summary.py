"""Structured outputs: agent decisions, qualification results and closure summaries."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

UNKNOWN = "Unknown"


class Classification(str, Enum):
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"
    IRRELEVANT = "IRRELEVANT"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class ExtractedFacts(BaseModel):
    """Facts the customer stated. Only fields supported by the conversation are set."""

    machine_required: str | None = None
    application: str | None = None
    material_or_workpiece: str | None = None
    technical_requirements: str | None = None
    purchase_purpose: str | None = None  # new facility | expansion | replacement | other
    purchase_timeline: str | None = None
    buying_stage: str | None = None  # researching | comparing | quotation | ready_to_buy
    budget: str | None = None
    location: str | None = None
    wants_quote: bool | None = None
    wants_demo: bool | None = None
    wants_call: bool | None = None
    wants_site_visit: bool | None = None
    objections: list[str] = Field(default_factory=list)


class AgentDecision(BaseModel):
    reply_text: str
    intent: str = "other"
    products_discussed: list[str] = Field(default_factory=list)
    quoted_product_ids: list[str] = Field(default_factory=list)
    facts: ExtractedFacts = Field(default_factory=ExtractedFacts)
    questions_asked: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    handover_required: bool = False
    handover_reason: str | None = None
    conversation_complete: bool = False
    irrelevant: bool = False
    guard_notes: list[str] = Field(default_factory=list)


class QualificationResult(BaseModel):
    classification: Classification
    score: int
    reasons: list[str]


class QuotedPrice(BaseModel):
    product_id: str | None
    model: str | None
    listed_price: float | None
    currency: str | None
    tax_treatment: str
    source_file: str | None
    source_reference: str | None
    price_valid_until: str | None


class LeadSummary(BaseModel):
    customer_name: str = UNKNOWN
    company: str = UNKNOWN
    original_enquiry: str = UNKNOWN
    detailed_requirement: str = UNKNOWN
    intended_application: str = UNKNOWN
    products_discussed: list[str] = Field(default_factory=list)
    verified_prices_quoted: list[QuotedPrice] = Field(default_factory=list)
    technical_requirements: str = UNKNOWN
    purchase_timeline: str = UNKNOWN
    budget: str = UNKNOWN
    purchase_intent: str = UNKNOWN
    objections: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    lead_classification: str = UNKNOWN
    classification_reasons: list[str] = Field(default_factory=list)
    agreed_follow_up: str = UNKNOWN
    recommended_next_action: str = UNKNOWN
    human_escalation_required: bool = False
    human_escalation_reason: str | None = None
    conversation_started_at: str = UNKNOWN
    conversation_ended_at: str = UNKNOWN
    close_reason: str = UNKNOWN

    def to_text(self) -> str:
        from app.models.product import format_amount

        prices = "; ".join(
            f"{q.model or q.product_id}: {format_amount(q.listed_price, q.currency)} ({q.tax_treatment})"
            for q in self.verified_prices_quoted if q.listed_price is not None
        ) or "None"
        parts = [
            f"Customer: {self.customer_name} ({self.company})",
            f"Enquiry: {self.original_enquiry}",
            f"Requirement: {self.detailed_requirement}",
            f"Application: {self.intended_application}",
            f"Products discussed: {', '.join(self.products_discussed) or 'None'}",
            f"Prices quoted: {prices}",
            f"Technical: {self.technical_requirements}",
            f"Timeline: {self.purchase_timeline}",
            f"Budget: {self.budget}",
            f"Intent: {self.purchase_intent}",
            f"Objections: {', '.join(self.objections) or 'None'}",
            f"Open questions: {', '.join(self.unanswered_questions) or 'None'}",
            f"Classification: {self.lead_classification}",
            f"Follow-up: {self.agreed_follow_up}",
            f"Next action: {self.recommended_next_action}",
            f"Escalation: {'Yes - ' + (self.human_escalation_reason or '') if self.human_escalation_required else 'No'}",
            f"Window: {self.conversation_started_at} -> {self.conversation_ended_at}",
        ]
        return " | ".join(parts)

    def model_dump_public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
