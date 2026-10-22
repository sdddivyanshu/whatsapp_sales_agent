"""Closure summaries.

Claude writes the narrative fields from the transcript; the system then
overlays everything that must be exact - identity fields, timestamps, the
qualification result, and *verified* prices taken from the quote audit log
(never from the model). Free-text fields containing amounts that were not
stated by the customer or verified are reset to ``Unknown``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.guardrails import extract_amounts
from app.agents.system_prompt import build_summary_system_prompt
from app.config import BusinessProfile
from app.integrations.anthropic_client import LLMClient
from app.models.summary import UNKNOWN, Classification, LeadSummary, QualificationResult, QuotedPrice

log = logging.getLogger(__name__)

SUMMARY_TOOL = "write_lead_summary"
_TEXT_FIELDS = ["detailed_requirement", "intended_application", "technical_requirements",
                "purchase_timeline", "budget", "purchase_intent", "agreed_follow_up",
                "recommended_next_action"]
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{f: {"type": "string"} for f in _TEXT_FIELDS},
        "objections": {"type": "array", "items": {"type": "string"}},
        "unanswered_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": _TEXT_FIELDS,
}


def suggested_next_action(qualification: QualificationResult | None, memory: dict[str, Any],
                          lead_status: str, close_reason: str | None) -> str:
    facts = memory.get("facts", {}) or {}
    handover = memory.get("handover") or {}
    if lead_status == "DO_NOT_CONTACT":
        return "Do not contact (customer opted out)"
    if handover.get("active"):
        return f"Salesperson to take over: {handover.get('reason')}"
    if qualification is None:
        return "Review conversation"
    cls = qualification.classification
    if cls == Classification.NEEDS_HUMAN_REVIEW:
        return "Sales manager to review the conversation"
    if cls == Classification.IRRELEVANT:
        return "No sales action required"
    actions = []
    if facts.get("wants_quote"):
        actions.append("send formal quotation")
    if facts.get("wants_demo"):
        actions.append("schedule a demo")
    if facts.get("wants_site_visit"):
        actions.append("arrange a site visit")
    if facts.get("wants_call"):
        actions.append("call the customer")
    if cls == Classification.HOT:
        return ("Priority: " + ", ".join(actions or ["call the customer"]) + " within 24 hours").capitalize()
    if cls == Classification.WARM:
        return ", ".join(actions or ["follow up in 1 week to confirm specifications and timeline"]).capitalize()
    if close_reason == "no_response":
        return "Customer did not reply; retry with an approved template after the follow-up interval"
    return "Add to nurture list; follow up in 1-2 months"


class Summarizer:
    def __init__(self, llm: LLMClient, profile: BusinessProfile) -> None:
        self.llm = llm
        self.profile = profile

    def summarize(self, *, lead: dict[str, Any], conversation: dict[str, Any],
                  transcript: list[dict[str, str]], memory: dict[str, Any],
                  qualification: QualificationResult | None, quotes: list[dict[str, Any]],
                  close_reason: str, closed_at: str) -> LeadSummary:
        suggested = suggested_next_action(qualification, memory, lead.get("status", ""), close_reason)
        lines = []
        for item in transcript:
            who = {"customer": "Customer", "agent": "Agent", "human": "Salesperson"}.get(item["role"], "System")
            text = (item["text"] or "").replace("<", "‹").replace(">", "›")
            lines.append(f"{who}: {text}")
        context = {
            "lead": {"customer_name": lead.get("customer_name"), "company": lead.get("company_name"),
                     "requirement": lead.get("requirement")},
            "memory": memory,
            "suggested_next_action": suggested,
        }
        user = (
            "<lead_record>\n" + "\n".join(f"{k}: {v or 'Unknown'}" for k, v in context["lead"].items())
            + "\n</lead_record>\n\n<conversation_history>\n" + "\n".join(lines)
            + "\n</conversation_history>\n\n"
            f"System-suggested next action (use unless the transcript clearly agreed something else): "
            f"{suggested}\nWrite the summary now."
        )
        raw = self.llm.structured(
            system=build_summary_system_prompt(self.profile),
            messages=[{"role": "user", "content": user}], tool_name=SUMMARY_TOOL,
            tool_description="Record the CRM summary of this conversation.", schema=SUMMARY_SCHEMA,
            context=context)

        customer_text = " ".join(i["text"] or "" for i in transcript if i["role"] == "customer")
        customer_amounts = set(extract_amounts(customer_text))
        facts = memory.get("facts", {}) or {}
        cleaned: dict[str, Any] = {}
        for field in _TEXT_FIELDS:
            value = str(raw.get(field) or "").strip() or UNKNOWN
            amounts = set(extract_amounts(value))
            if amounts - customer_amounts:
                log.warning("Summary field %s contained unverified amounts; set to Unknown", field)
                value = UNKNOWN
            cleaned[field] = value
        # Budget/timeline must be supported by what the customer actually said.
        if facts.get("budget"):
            cleaned["budget"] = facts["budget"]
        elif cleaned["budget"] != UNKNOWN and not _supported(cleaned["budget"], customer_text):
            cleaned["budget"] = UNKNOWN
        if facts.get("purchase_timeline"):
            cleaned["purchase_timeline"] = facts["purchase_timeline"]
        elif cleaned["purchase_timeline"] != UNKNOWN and not _supported(
                cleaned["purchase_timeline"], customer_text):
            cleaned["purchase_timeline"] = UNKNOWN

        seen: set[str] = set()
        prices: list[QuotedPrice] = []
        for q in quotes:
            key = f"{q.get('product_id')}|{q.get('model')}|{q.get('listed_price')}"
            if key in seen:
                continue
            seen.add(key)
            tax = q.get("tax_included")
            rate = f" @ {q['tax_rate']:g}%" if q.get("tax_rate") is not None else ""
            treatment = ("GST included" + rate) if tax == 1 else ("GST extra" + rate) if tax == 0 \
                else "GST treatment not specified"
            prices.append(QuotedPrice(
                product_id=q.get("product_id"), model=q.get("model"), listed_price=q.get("listed_price"),
                currency=q.get("currency"), tax_treatment=treatment, source_file=q.get("source_file"),
                source_reference=q.get("source_reference"), price_valid_until=q.get("price_valid_until")))

        handover = memory.get("handover") or {}
        return LeadSummary(
            customer_name=lead.get("customer_name") or UNKNOWN,
            company=lead.get("company_name") or UNKNOWN,
            original_enquiry=lead.get("requirement") or UNKNOWN,
            products_discussed=list(memory.get("products_of_interest") or []),
            verified_prices_quoted=prices,
            objections=[str(o) for o in (raw.get("objections") or facts.get("objections") or [])],
            unanswered_questions=[str(q) for q in (raw.get("unanswered_questions")
                                                   or memory.get("pending_questions") or [])],
            lead_classification=qualification.classification.value if qualification else UNKNOWN,
            classification_reasons=qualification.reasons if qualification else [],
            human_escalation_required=bool(handover.get("active") or handover.get("reason")),
            human_escalation_reason=handover.get("reason"),
            conversation_started_at=conversation.get("opened_at") or UNKNOWN,
            conversation_ended_at=closed_at or UNKNOWN,
            close_reason=close_reason,
            **cleaned,
        )


def _supported(value: str, customer_text: str) -> bool:
    """Every number / significant word of ``value`` must appear in the customer's own text."""
    text = customer_text.lower()
    numbers = re.findall(r"\d+", value)
    if numbers:
        return all(n in text for n in numbers)
    words = [w for w in re.findall(r"[a-z]{4,}", value.lower())
             if w not in {"within", "around", "about", "planning", "purchase", "months", "month"}]
    return any(w in text for w in words) if words else False
