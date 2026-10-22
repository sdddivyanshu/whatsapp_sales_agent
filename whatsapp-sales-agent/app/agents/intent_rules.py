"""Deterministic intent signals that must not depend on the LLM.

Opt-outs, discount requests, explicit requests for a human and complaints are
detected with plain rules so they are handled correctly even if the model
misreads the message or the message tries to manipulate it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FLAGS = re.IGNORECASE

OPT_OUT = re.compile(
    r"^\s*(stop|unsubscribe|opt[\s-]?out)\s*[.!]*\s*$"
    r"|\b(don'?t|do not|dont|never)\s+(contact|message|msg|text|call|whatsapp|disturb)\s*(me|us)?\b"
    r"|\b(remove|delete)\s+(me|my\s+(number|details|data))\b"
    r"|\bno\s+more\s+(messages|msgs|calls)\b|\bstop\s+(messaging|sending|contacting|calling)\b",
    _FLAGS)

DECLINE = re.compile(
    r"\b(not\s+interested|no\s+longer\s+(interested|required|needed)|not\s+required\s+(now|anymore)"
    r"|already\s+(bought|purchased|ordered)|we\s+don'?t\s+need|no\s+requirement)\b", _FLAGS)

FINISHED = re.compile(
    r"\b(that'?s\s+all|that\s+is\s+all|nothing\s+else|no\s+(more|other)\s+questions"
    r"|good\s*bye|bye\s*bye|bye|talk\s+later|will\s+get\s+back\s+to\s+you)\b", _FLAGS)

HUMAN_REQUEST = re.compile(
    r"\b(talk|speak|connect|chat)\s+(to|with)\s+(a\s+|an\s+|your\s+|the\s+)?"
    r"(human|person|real\s+person|someone|somebody|sales\s*(person|man|team|executive|rep)"
    r"|executive|representative|manager|engineer|agent)\b"
    r"|\b(human|real\s+person)\s+please\b|\bare\s+you\s+a\s+(bot|robot|machine)\b", _FLAGS)

DISCOUNT = re.compile(
    r"\b(discount|discounts|best\s+price|lowest\s+price|final\s+price|last\s+price|negotiat\w*"
    r"|reduce\s+(the\s+)?price|price\s+reduction|cheaper|concession|special\s+price|any\s+offer"
    r"|offer\s+price|kam\s+karo|thoda\s+kam|less\s+price)\b", _FLAGS)

COMPLAINT = re.compile(
    r"\b(complain\w*|not\s+working|breakdown|broke\s+down|defect\w*|repair|service\s+(issue|problem)"
    r"|problem\s+with\s+(my|our|the)\s+machine|refund|legal\s+action)\b", _FLAGS)

PRICE = re.compile(r"\b(price|prices|pricing|cost|rate|rates|quotation|quote|how\s+much|kitna|budgetary)\b",
                   _FLAGS)

CALL_REQUEST = re.compile(r"\b(call\s+me|give\s+(me\s+)?a\s+call|phone\s+call|call\s+back|callback)\b",
                          _FLAGS)

INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier)?\s*"
    r"(instructions|rules|prompts?)\b|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\bdeveloper\s+mode\b"
    r"|\b(api|access)\s+(key|token)\b|\bjailbreak\b|\bact\s+as\b", _FLAGS)


@dataclass
class RuleSignals:
    opt_out: bool = False
    declined: bool = False
    finished: bool = False
    human_request: bool = False
    discount_request: bool = False
    complaint: bool = False
    price_enquiry: bool = False
    call_request: bool = False
    injection_attempt: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def handover_reason(self) -> str | None:
        if self.human_request:
            return "Customer asked to speak to a person"
        if self.discount_request:
            return "Discount / negotiated price requested"
        if self.complaint:
            return "Customer reported an issue or complaint"
        return None


def detect(text: str) -> RuleSignals:
    s = RuleSignals(
        opt_out=bool(OPT_OUT.search(text)),
        declined=bool(DECLINE.search(text)),
        finished=bool(FINISHED.search(text)),
        human_request=bool(HUMAN_REQUEST.search(text)),
        discount_request=bool(DISCOUNT.search(text)),
        complaint=bool(COMPLAINT.search(text)),
        price_enquiry=bool(PRICE.search(text)),
        call_request=bool(CALL_REQUEST.search(text)),
        injection_attempt=bool(INJECTION.search(text)),
    )
    if s.injection_attempt:
        s.notes.append("message contains instruction-like text; treated as untrusted data")
    return s
