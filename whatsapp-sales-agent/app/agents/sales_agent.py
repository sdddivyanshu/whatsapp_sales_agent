"""The WhatsApp sales agent: grounded retrieval -> Claude -> deterministic guardrails.

Per inbound turn:
1. Deterministic rule signals (opt-out, discount, human request, complaint...).
2. Exact model matching and price verification against the structured price list.
3. Keyword retrieval of catalogue text for explanatory answers.
4. One structured Claude call (``respond_to_customer`` tool) with all untrusted
   text clearly delimited.
5. Deterministic overrides (handover, completion) and reply guardrails
   (unverified amounts, discount promises, foreign contact details, leaks).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.agents import intent_rules
from app.agents.guardrails import GuardResult, check_reply, extract_amounts
from app.agents.system_prompt import build_sales_system_prompt
from app.config import BusinessProfile
from app.integrations.anthropic_client import LLMClient, LLMError
from app.knowledge.document_loader import parse_price
from app.knowledge.price_lookup import PriceCheck, PriceLookup, PriceStatus
from app.knowledge.retrieval import Retriever
from app.models.product import Product, model_key
from app.models.summary import AgentDecision

log = logging.getLogger(__name__)

RESPOND_TOOL = "respond_to_customer"

# Looks like a machine model code: letters followed by digits, e.g. "WC-400S", "XT 9000".
_MATERIAL_GRADES = {"H13", "H11", "EN8", "EN9", "EN19", "EN24", "EN31", "EN353", "SS304", "SS316",
                    "SS410", "P20", "D2", "D3", "M2", "OHNS", "AISI4140", "SKD11", "SKD61", "AL6061",
                    "AL7075", "DIN1", "IS2062"}
_MODEL_CODE_RE = re.compile(r"\b[A-Za-z]{1,5}[- ]?\d{2,5}[A-Za-z]{0,2}\b")

_STR_OR_NULL = {"type": ["string", "null"]}
_BOOL_OR_NULL = {"type": ["boolean", "null"]}

RESPOND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string", "description": "The WhatsApp message to send."},
        "intent": {"type": "string", "enum": [
            "greeting", "price_enquiry", "technical_question", "comparison", "discount_request",
            "timeline", "objection", "call_request", "demo_request", "human_request",
            "closing", "irrelevant", "other"]},
        "products_discussed": {"type": "array", "items": {"type": "string"},
                               "description": "Model codes discussed in this turn."},
        "quoted_product_ids": {"type": "array", "items": {"type": "string"}},
        "facts": {
            "type": "object",
            "description": "Only facts the CUSTOMER stated; null when not stated.",
            "properties": {
                "machine_required": _STR_OR_NULL, "application": _STR_OR_NULL,
                "material_or_workpiece": _STR_OR_NULL, "technical_requirements": _STR_OR_NULL,
                "purchase_purpose": _STR_OR_NULL, "purchase_timeline": _STR_OR_NULL,
                "buying_stage": {"type": ["string", "null"], "enum": [
                    "researching", "comparing", "quotation", "ready_to_buy", None]},
                "budget": _STR_OR_NULL, "location": _STR_OR_NULL,
                "wants_quote": _BOOL_OR_NULL, "wants_demo": _BOOL_OR_NULL,
                "wants_call": _BOOL_OR_NULL, "wants_site_visit": _BOOL_OR_NULL,
                "objections": {"type": "array", "items": {"type": "string"}},
            },
        },
        "questions_asked": {"type": "array", "items": {"type": "string", "enum": [
            "machine", "application", "technical", "purpose", "timeline", "buying_stage",
            "next_step", "location", "configuration"]}},
        "unanswered_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "Customer questions you could not answer."},
        "handover_required": {"type": "boolean"},
        "handover_reason": _STR_OR_NULL,
        "conversation_complete": {"type": "boolean"},
        "irrelevant": {"type": "boolean",
                       "description": "True only if the enquiry is unrelated to our business."},
    },
    "required": ["reply_text", "intent", "facts", "handover_required", "conversation_complete"],
}


def _untrusted(text: str) -> str:
    """Neutralise tag delimiters so untrusted text cannot close our XML-style blocks."""
    return (text or "").replace("<", "‹").replace(">", "›")


@dataclass
class AgentTurn:
    lead: dict[str, Any]
    memory: dict[str, Any]
    history: list[dict[str, str]]       # [{"role": "customer"|"agent"|"human", "text": ...}]
    new_messages: list[str]


@dataclass
class AgentResult:
    decision: AgentDecision
    rules: intent_rules.RuleSignals
    price_checks: list[PriceCheck]
    quoted: list[PriceCheck]
    guard: GuardResult
    model_keys: list[str] = field(default_factory=list)
    llm_used: bool = True


class SalesAgent:
    def __init__(self, llm: LLMClient, profile: BusinessProfile, price_lookup: PriceLookup,
                 retriever: Retriever, secrets: list[str] | None = None,
                 company_phones: list[str] | None = None, history_limit: int = 20) -> None:
        self.llm = llm
        self.profile = profile
        self.price_lookup = price_lookup
        self.retriever = retriever
        self.secrets = secrets or []
        self.company_phones = [p for p in (company_phones or []) if p]
        self.history_limit = history_limit

    # ------------------------------------------------------------------ helpers
    def _allowed_contacts(self) -> tuple[list[str], list[str]]:
        phones = list(self.company_phones)
        for key in ("contact.sales_phone", "contact.escalation_phone", "contact.whatsapp_number"):
            if v := self.profile.known(key):
                phones.append(str(v))
        emails = [str(v) for key in ("contact.sales_email", "contact.escalation_email")
                  if (v := self.profile.known(key))]
        return phones, emails

    def fallback_reply(self) -> str:
        return self.profile.known("conversation.fallback_reply") or (
            "Thank you for your message. Let me check this with our sales team and get back "
            "to you shortly.")

    def opt_out_reply(self) -> str:
        return self.profile.known("conversation.opt_out_confirmation") or (
            "Understood. We will not send you any further messages. Thank you for your time.")

    def _price_checks(self, text: str, memory: dict[str, Any], rules: intent_rules.RuleSignals,
                      requirement: str) -> tuple[list[str], list[PriceCheck], list[Product]]:
        keys = self.price_lookup.match_models(text)
        context_products = self.price_lookup.products_for_text(f"{text} {requirement}")
        # Model-like codes the customer typed that are not in our price list
        # (e.g. "XT-9000"): answer "not found" instead of substituting other models.
        unknown_codes = [
            m.group(0) for m in _MODEL_CODE_RE.finditer(text)
            if rules.price_enquiry and model_key(m.group(0)) not in _MATERIAL_GRADES
            and model_key(m.group(0)) not in keys and not any(
                model_key(m.group(0)) in k or k in model_key(m.group(0)) for k in keys)]
        checks: list[PriceCheck] = [PriceCheck(PriceStatus.NOT_FOUND, code, reason="model not in price list")
                                    for code in unknown_codes if not keys]
        if not keys and not unknown_codes and rules.price_enquiry:
            keys = list(memory.get("product_keys", []))[-2:]
            if not keys:
                keys = list(dict.fromkeys(p.key for p in context_products if p.key))[:4]
        checks += [self.price_lookup.check(k, text) for k in keys]
        return keys, checks, context_products

    def _render_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = [{"role": "user", "content":
                                       "[Conversation started with our outreach message below.]"}]
        for item in history[-self.history_limit:]:
            if item["role"] == "customer":
                role, content = "user", f"<customer_message>{_untrusted(item['text'])}</customer_message>"
            else:
                role, content = "assistant", item["text"]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + content
            else:
                msgs.append({"role": role, "content": content})
        return msgs

    # ------------------------------------------------------------------ main
    def respond(self, turn: AgentTurn) -> AgentResult:
        text = "\n".join(turn.new_messages).strip()
        rules = intent_rules.detect(text)
        requirement = turn.lead.get("requirement") or ""

        if rules.opt_out:
            decision = AgentDecision(reply_text=self.opt_out_reply(), intent="opt_out",
                                     conversation_complete=True)
            guard = GuardResult(decision.reply_text, True)
            return AgentResult(decision, rules, [], [], guard, llm_used=False)

        keys, checks, context_products = self._price_checks(text, turn.memory, rules, requirement)
        product_facts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in keys:
            for p in self.price_lookup.repo.by_model_key(key):
                ident = f"{p.key}|{p.configuration}"
                if ident not in seen:
                    seen.add(ident)
                    product_facts.append(p.facts_for_prompt(include_price=False))
        for p in context_products:
            ident = f"{p.key}|{p.configuration}"
            if ident not in seen and len(product_facts) < 8:
                seen.add(ident)
                product_facts.append(p.facts_for_prompt(include_price=False))
        snippets = self.retriever.search(f"{text} {requirement}", k=4)

        context = {
            "lead": {k: turn.lead.get(k) or "Unknown" for k in
                     ("customer_name", "company_name", "requirement", "city", "state", "industry")},
            "memory": {k: turn.memory.get(k) for k in
                       ("facts", "products_of_interest", "questions_asked", "prices_shared",
                        "pending_questions") if turn.memory.get(k)},
            "rules": {k: v for k, v in rules.__dict__.items() if v and k != "notes"},
            "handover_reason": rules.handover_reason,
            "price_checks": [c.as_fact() for c in checks],
            "products": product_facts,
            "snippets": [{"ref": f"doc-{i + 1}", "text": s.text[:700]} for i, s in enumerate(snippets)],
            "new_text": text,
            "customer_name": turn.lead.get("customer_name") or "",
        }

        user_block = "\n\n".join([
            f"<customer_message>\n{_untrusted(text)}\n</customer_message>",
            f"<lead_record>\n{json.dumps(context['lead'], ensure_ascii=False)}\n</lead_record>",
            f"<memory>\n{json.dumps(context['memory'], ensure_ascii=False, default=str)}\n</memory>",
            f"<rule_signals>\n{json.dumps(context['rules'])}\n"
            + (f"Handover will be arranged by the system: {rules.handover_reason}. Tell the customer "
               "an authorised salesperson will contact them; do not negotiate.\n"
               if rules.handover_reason else "") + "</rule_signals>",
            f"<product_facts>\n{json.dumps(product_facts, ensure_ascii=False)}\n</product_facts>",
            f"<verified_price_checks>\n{json.dumps(context['price_checks'], ensure_ascii=False)}\n"
            "</verified_price_checks>",
            "<reference_documents>\n" + "\n---\n".join(
                f"[{s['ref']}] {_untrusted(s['text'])}" for s in context["snippets"])
            + "\n</reference_documents>",
            "Write the next WhatsApp reply now using the respond_to_customer tool.",
        ])
        messages = self._render_history(turn.history)
        if messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n\n" + user_block
        else:
            messages.append({"role": "user", "content": user_block})

        raw = self.llm.structured(
            system=build_sales_system_prompt(self.profile), messages=messages,
            tool_name=RESPOND_TOOL, tool_description="Send the next WhatsApp reply and record facts.",
            schema=RESPOND_SCHEMA, context=context)
        try:
            decision = AgentDecision.model_validate(_normalise_decision(raw))
        except ValidationError as exc:
            raise LLMError(f"Invalid agent output: {exc}") from exc

        # ---- deterministic overrides ------------------------------------
        if rules.handover_reason:
            decision.handover_required = True
            decision.handover_reason = rules.handover_reason
        conflicts = [c for c in checks if c.status == PriceStatus.CONFLICT]
        if conflicts:
            decision.handover_required = True
            decision.handover_reason = decision.handover_reason or (
                "Conflicting price-list entries for " + ", ".join(c.requested for c in conflicts))
        if rules.call_request:
            decision.facts.wants_call = True
        if rules.finished or rules.declined:
            decision.conversation_complete = True
        if decision.irrelevant and (keys or turn.memory.get("products_of_interest")):
            decision.irrelevant = False  # never irrelevant while discussing our products

        # ---- guardrails ---------------------------------------------------
        allowed: set[float] = set()
        for c in checks:
            if c.quotable and c.product is not None:
                allowed.add(float(c.product.listed_price or 0))
                for extra in (c.product.installation_charges, c.product.shipping_charges):
                    if (amount := parse_price(extra)) is not None:
                        allowed.add(amount)
        for item in turn.history:
            if item["role"] == "customer":
                allowed.update(extract_amounts(item["text"]))
        allowed.update(extract_amounts(text))
        phones, emails = self._allowed_contacts()
        guard = check_reply(decision.reply_text, allowed_amounts=allowed, allowed_phones=phones,
                            allowed_emails=emails, secrets=self.secrets,
                            fallback=self.fallback_reply())
        if not guard.passed:
            log.warning("Reply guard replaced the model reply: %s", guard.violations)
            decision.guard_notes.extend(guard.violations)
        decision.reply_text = guard.text
        amounts_in_reply = set(extract_amounts(guard.text))
        quoted = [c for c in checks if c.quotable and c.product is not None
                  and float(c.product.listed_price or 0) in amounts_in_reply]
        decision.quoted_product_ids = [c.product.product_id or c.product.model or c.requested
                                       for c in quoted if c.product is not None]
        return AgentResult(decision, rules, checks, quoted, guard, model_keys=keys)


def _normalise_decision(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw or {})
    facts = data.get("facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    facts["objections"] = [o for o in (facts.get("objections") or []) if isinstance(o, str)]
    data["facts"] = facts
    for key in ("products_discussed", "quoted_product_ids", "questions_asked", "unanswered_questions"):
        data[key] = [str(x) for x in (data.get(key) or [])]
    data["handover_required"] = bool(data.get("handover_required"))
    data["conversation_complete"] = bool(data.get("conversation_complete"))
    data["irrelevant"] = bool(data.get("irrelevant"))
    data["reply_text"] = str(data.get("reply_text") or "")
    return data
