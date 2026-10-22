"""Transparent, configurable lead qualification.

The LLM only *extracts facts* (timeline, application, requests...). The score,
thresholds and final classification are computed here from
``config/qualification_rules.yaml`` so the business logic is auditable and
editable, and every classification carries its reasons.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.summary import Classification, QualificationResult

DEFAULT_RULES: dict[str, Any] = {
    "weights": {
        "requirement_clear": 25,
        "application_known": 15,
        "technical_requirements_known": 10,
        "purpose_known": 5,
        "timeline_near": 25,
        "timeline_mid": 10,
        "sales_action_requested": 20,
        "ready_to_buy_stage": 15,
    },
    "thresholds": {"hot": 70, "warm": 40},
    "timeline": {"near_term_months": 3, "mid_term_months": 6},
    "hot_requires": {"near_term_timeline": True, "sales_action_requested": True},
    "human_review_triggers": ["price_conflict", "complaint", "repeated_guard_violation"],
}

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "nine": 9,
              "twelve": 12, "a": 1, "an": 1}


def timeline_months(text: str | None) -> float | None:
    """Rough number of months until purchase, or None when unknown/undecided."""
    if not text:
        return None
    t = text.lower()
    if re.search(r"not\s+sure|no\s+(plan|timeline|idea)|just\s+(checking|looking|researching)"
                 r"|researching|someday|in\s+future|don'?t\s+know|undecided", t):
        return None
    if re.search(r"immediate|urgent|asap|right\s+away|this\s+week|next\s+week|within\s+days", t):
        return 0.25
    if re.search(r"this\s+month", t):
        return 1
    if re.search(r"next\s+month", t):
        return 2
    if re.search(r"this\s+quarter", t):
        return 3
    if re.search(r"next\s+quarter", t):
        return 6
    if re.search(r"next\s+(year|financial\s+year|fy)", t):
        return 12
    m = re.search(r"\b(\d+|one|two|three|four|five|six|nine|twelve|a|an)\s*(?:-|to)?\s*(\d+)?\s*"
                  r"(day|days|week|weeks|month|months|year|years)", t)
    if m:
        first = m.group(1)
        n = float(m.group(2) or (int(first) if first.isdigit() else _NUM_WORDS.get(first, 1)))
        unit = m.group(3)
        if unit.startswith("day"):
            return n / 30
        if unit.startswith("week"):
            return n / 4
        if unit.startswith("year"):
            return n * 12
        return n
    return None


def _merge_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_RULES.items()}
    for key, value in (rules or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


class QualificationEngine:
    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self.rules = _merge_rules(rules)

    def evaluate(self, memory: dict[str, Any], lead_requirement: str | None = None) -> QualificationResult:
        facts: dict[str, Any] = memory.get("facts", {}) or {}
        flags: dict[str, Any] = memory.get("flags", {}) or {}
        weights = self.rules["weights"]
        reasons: list[str] = []

        review_triggers = set(self.rules.get("human_review_triggers", []))
        review_hits = [t for t in review_triggers if flags.get(t)]
        if review_hits:
            return QualificationResult(
                classification=Classification.NEEDS_HUMAN_REVIEW, score=0,
                reasons=[f"Needs review: {t.replace('_', ' ')}" for t in sorted(review_hits)])

        products = memory.get("products_of_interest") or []
        if flags.get("irrelevant") and not products:
            return QualificationResult(classification=Classification.IRRELEVANT, score=0,
                                       reasons=["Enquiry is unrelated to our products/services"])

        score = 0
        requirement = facts.get("machine_required") or (products and ", ".join(products)) \
            or lead_requirement
        if requirement:
            score += weights["requirement_clear"]
            reasons.append(f"Clear requirement: {requirement}")
        if facts.get("application") or facts.get("material_or_workpiece"):
            score += weights["application_known"]
            reasons.append("Application/workpiece described")
        if facts.get("technical_requirements"):
            score += weights["technical_requirements_known"]
            reasons.append("Technical requirements shared")
        if facts.get("purchase_purpose"):
            score += weights["purpose_known"]
            reasons.append(f"Purpose: {facts['purchase_purpose']}")

        months = timeline_months(facts.get("purchase_timeline"))
        near = months is not None and months <= self.rules["timeline"]["near_term_months"]
        if near:
            score += weights["timeline_near"]
            reasons.append(f"Near-term timeline: {facts.get('purchase_timeline')}")
        elif months is not None and months <= self.rules["timeline"]["mid_term_months"]:
            score += weights["timeline_mid"]
            reasons.append(f"Mid-term timeline: {facts.get('purchase_timeline')}")
        elif facts.get("purchase_timeline"):
            reasons.append(f"Distant/undefined timeline: {facts.get('purchase_timeline')}")
        else:
            reasons.append("Purchase timeline unknown")

        requested = [name for name, key in (("quotation", "wants_quote"), ("demo", "wants_demo"),
                                            ("call", "wants_call"), ("site visit", "wants_site_visit"))
                     if facts.get(key)]
        if requested:
            score += weights["sales_action_requested"]
            reasons.append("Requested " + ", ".join(requested))
        stage = (facts.get("buying_stage") or "").lower()
        if stage in {"ready_to_buy", "ready to buy", "quotation"}:
            score += weights["ready_to_buy_stage"]
            reasons.append(f"Buying stage: {stage}")

        hot_req = self.rules["hot_requires"]
        hot_ok = score >= self.rules["thresholds"]["hot"] \
            and (near or not hot_req.get("near_term_timeline")) \
            and (bool(requested) or not hot_req.get("sales_action_requested"))
        if hot_ok:
            cls = Classification.HOT
        elif score >= self.rules["thresholds"]["warm"]:
            cls = Classification.WARM
        else:
            cls = Classification.COLD
        return QualificationResult(classification=cls, score=score, reasons=reasons)
