"""Deterministic fixture "LLM" used in mock mode and tests.

It produces realistic, grounded replies from the same context the real model
receives (verified price checks, product facts, memory), with simple regex fact
extraction. It lets the full pipeline run offline without an API key. Real
deployments use Claude (``LLM_MODE=live``).
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.sales_agent import RESPOND_TOOL
from app.agents.summarizer import SUMMARY_TOOL
from app.integrations.anthropic_client import MockLLMClient
from app.models.product import format_amount

_MATERIALS = r"(die steel|tool steel|stainless steel|mild steel|steel|carbide|tungsten carbide|" \
             r"aluminium|aluminum|copper|brass|titanium|inconel|graphite|d2|hss|h13)"


def extract_facts(text: str) -> dict[str, Any]:
    t = text.lower()
    facts: dict[str, Any] = {}
    if m := re.search(r"\b(immediately|urgent(ly)?|asap|this month|next month|this quarter|next quarter"
                      r"|next year|within (a|one|two|three|\d+) (weeks?|months?)"
                      r"|in (a|one|two|three|\d+) (weeks?|months?)|\d+\s*-\s*\d+ months"
                      r"|just (checking|researching|looking)|not sure)\b", t):
        facts["purchase_timeline"] = m.group(0)
    if m := re.search(_MATERIALS, t):
        facts["material_or_workpiece"] = m.group(0)
    if re.search(r"\b(die|dies|mould|mold|moulds|molds|punch|punches|components?|parts|tool ?room|"
                 r"aerospace|automotive|medical|jobwork|job work|extrusion)\b", t):
        facts["application"] = re.sub(r"^\s*(hi|hello|hey|namaste)\b[,!. ]*", "", text.strip(),
                                      flags=re.I)[:160]
    tech = re.findall(r"\d+\s*(?:mm|micron|microns|µm|um|degree|°|amp|a)\b", t)
    if tech or re.search(r"\b(accuracy|taper|surface finish|travel)\b", t):
        facts["technical_requirements"] = ", ".join(tech) or text.strip()[:120]
    if re.search(r"new (plant|facility|unit|factory|setup)", t):
        facts["purchase_purpose"] = "new facility"
    elif re.search(r"expan|additional machine|more capacity|capacity", t):
        facts["purchase_purpose"] = "capacity expansion"
    elif re.search(r"replac|old machine", t):
        facts["purchase_purpose"] = "replacement"
    if re.search(r"compar|other vendors|other suppliers", t):
        facts["buying_stage"] = "comparing"
    elif re.search(r"ready to (buy|order)|place (the |an )?order|\bpo\b|finali[sz]e", t):
        facts["buying_stage"] = "ready_to_buy"
    elif re.search(r"just (checking|researching|looking)|research", t):
        facts["buying_stage"] = "researching"
    if re.search(r"quotation|quote|send (the |me )?(price|offer)", t):
        facts["wants_quote"] = True
        facts.setdefault("buying_stage", "quotation")
    if re.search(r"\bdemo|demonstration|trial\b", t):
        facts["wants_demo"] = True
    if re.search(r"site visit|visit (our|my) (factory|plant|unit)", t):
        facts["wants_site_visit"] = True
    if m := re.search(r"budget[^0-9₹]{0,20}((?:₹|rs\.?|inr)?\s*[\d.,]+\s*(?:lakhs?|lacs?|crores?|cr)?)", t):
        facts["budget"] = m.group(1).strip()
    if m := re.search(r"\b(?:located|based|factory|plant|unit)\s+(?:is\s+)?in\s+([a-z][a-z ]{2,30})", t):
        facts["location"] = m.group(1).strip().title()
    if re.search(r"\b(objection|too expensive|costly|high price)\b", t):
        facts["objections"] = ["Price perceived as high"]
    return facts


_QUESTIONS = [
    ("application", lambda f: f.get("application") or f.get("material_or_workpiece"),
     "Could you tell me what kind of components or material you will be machining?"),
    ("technical", lambda f: f.get("technical_requirements"),
     "What maximum job size / thickness and accuracy do you need?"),
    ("purpose", lambda f: f.get("purchase_purpose"),
     "Is this for a new setup, capacity expansion or replacing an existing machine?"),
    ("timeline", lambda f: f.get("purchase_timeline"),
     "By when are you planning to purchase?"),
    ("next_step", lambda f: f.get("wants_quote") or f.get("wants_demo") or f.get("wants_call"),
     "Would you like a formal quotation, a demo, or a call with our technical team?"),
]


def mock_respond(system: str, messages: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    text = ctx.get("new_text", "")
    rules = ctx.get("rules", {})
    memory = ctx.get("memory", {})
    facts = extract_facts(text)
    known = {**(memory.get("facts") or {}), **facts}
    name = (ctx.get("customer_name") or "").split(" ")[0]
    salutation = f"{name} ji" if name else "Sir/Ma'am"
    parts: list[str] = []
    discussed: list[str] = []
    asked: list[str] = []
    unanswered: list[str] = []
    intent = "other"

    if re.search(r"\b(job|vacancy|resume|loan|insurance|lottery)\b", text.lower()) and not ctx.get("price_checks"):
        return {"reply_text": "Thank you for reaching out. We deal in industrial machinery, so we may "
                              "not be able to help with this request.", "intent": "irrelevant",
                "facts": {}, "handover_required": False, "conversation_complete": False,
                "irrelevant": True}

    if rules.get("finished") or rules.get("declined"):
        return {"reply_text": f"Thank you for your time, {salutation}. Please feel free to message "
                              "us here whenever you need any help.",
                "intent": "closing", "facts": facts, "handover_required": False,
                "conversation_complete": True}

    if ctx.get("handover_reason"):
        intent = "discount_request" if rules.get("discount_request") else "human_request"
        parts.append(f"Thank you, {salutation}. I have shared your request with our sales team; "
                     "an authorised representative will contact you shortly.")

    already_shared = {p.get("model") for p in (memory.get("prices_shared") or [])}
    for check in ctx.get("price_checks", []):
        model = check.get("model") or check["model_requested"]
        discussed.append(model)
        named_now = model and model.replace("-", "").upper() in text.replace("-", "").replace(" ", "").upper()
        if model in already_shared and not named_now:
            continue  # do not repeat a price the customer already has
        status = check["price_status"]
        if status == "VERIFIED":
            intent = "price_enquiry" if intent == "other" else intent
            config = f" ({check['configuration']})" if check.get("configuration") not in (None, "Unknown") else ""
            line = (f"The listed price of {model}{config} is "
                    f"{format_amount(check['listed_price'], check['currency'])} - {check['tax']}.")
            extras = []
            if check.get("installation_charges") not in (None, "Unknown"):
                extras.append(f"installation: {check['installation_charges']}")
            if check.get("shipping_charges") not in (None, "Unknown"):
                extras.append(f"freight: {check['shipping_charges']}")
            line += (" " + "; ".join(extras)[:1].upper() + "; ".join(extras)[1:] + ".") if extras else \
                " Installation and freight will be confirmed in the formal quotation."
            parts.append(line)
        elif status == "AMBIGUOUS":
            parts.append(f"{model} is available as {' / '.join(check.get('configurations', []))}. "
                         "Which configuration are you interested in?")
            asked.append("configuration")
        else:
            parts.append(f"For {model}, our sales team will confirm the exact quotation for you.")
            unanswered.append(f"Price for {model}")

    if facts.get("wants_quote"):
        intent = "price_enquiry" if intent == "other" else intent
        parts.append("Noted - our sales team will prepare a formal quotation and share it with you.")
    elif not ctx.get("price_checks") and rules.get("price_enquiry"):
        unanswered.append("Price for unspecified model")
        parts.append("Our sales team will share the exact quotation once we know the model and "
                     "configuration you need.")

    if not parts and ctx.get("products"):
        p = ctx["products"][0]
        intent = "technical_question"
        spec = p.get("technical_specifications")
        if spec and spec != "Unknown":
            parts.append(f"Our {p.get('name')} ({p.get('model')}) offers {spec}.")
            discussed.append(p.get("model"))

    if not parts:
        intent = "greeting" if re.search(r"^\s*(hi|hello|hey|namaste)\b", text.lower()) else intent
        parts.append(f"Thank you, {salutation}.")

    if not asked and not ctx.get("handover_reason"):
        already = set(memory.get("questions_asked") or [])
        for key, satisfied, question in _QUESTIONS:
            if not satisfied(known) and key not in already:
                parts.append(question)
                asked.append(key)
                break

    return {
        "reply_text": " ".join(parts),
        "intent": intent,
        "products_discussed": [d for d in discussed if d],
        "facts": facts,
        "questions_asked": asked,
        "unanswered_questions": unanswered,
        "handover_required": bool(ctx.get("handover_reason")),
        "handover_reason": ctx.get("handover_reason"),
        "conversation_complete": False,
        "irrelevant": False,
    }


def mock_summary(system: str, messages: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    memory = ctx.get("memory", {})
    facts = memory.get("facts", {}) or {}
    lead = ctx.get("lead", {})
    unknown = "Unknown"
    stage = facts.get("buying_stage")
    return {
        "detailed_requirement": facts.get("machine_required") or lead.get("requirement") or unknown,
        "intended_application": facts.get("application") or facts.get("material_or_workpiece") or unknown,
        "technical_requirements": facts.get("technical_requirements") or unknown,
        "purchase_timeline": facts.get("purchase_timeline") or unknown,
        "budget": facts.get("budget") or unknown,
        "purchase_intent": stage.replace("_", " ") if stage else unknown,
        "objections": facts.get("objections") or [],
        "unanswered_questions": memory.get("pending_questions") or [],
        "agreed_follow_up": memory.get("follow_up") or unknown,
        "recommended_next_action": ctx.get("suggested_next_action") or unknown,
    }


def build_mock_llm() -> MockLLMClient:
    client = MockLLMClient()
    client.register(RESPOND_TOOL, mock_respond)
    client.register(SUMMARY_TOOL, mock_summary)
    return client
