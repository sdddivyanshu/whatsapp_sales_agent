"""Conversation lifecycle: inbound messages, agent replies, memory, handover and closure.

Concurrency & idempotency rules
-------------------------------
* Inbound messages are stored first (unique WhatsApp message id) and processed
  by a queued ``PROCESS_CONVERSATION`` job serialised per conversation.
* A reply is *claimed* in the same write transaction that checks
  ``automation_paused``; once a human has taken over, no automated reply can be
  claimed. The claimed reply row (status ``sending``) is re-used on retries.
* Several quick customer messages are answered with one reply.
* Each closure event increments ``closure_seq``; exactly one summary is stored
  per ``(conversation_id, closure_seq)``. A later inbound message reopens the
  conversation and the next closure produces a new summary.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from typing import Any

from app.agents.qualification import QualificationEngine
from app.agents.sales_agent import AgentResult, AgentTurn, SalesAgent
from app.agents.summarizer import Summarizer
from app.config import Settings
from app.integrations.anthropic_client import LLMError
from app.integrations.whatsapp_cloud import MessagingProvider, WhatsAppError
from app.logging_utils import mask_phone
from app.models.lead import LeadState
from app.models.message import InboundMessage, ParsedWebhook, StatusUpdate
from app.models.product import model_key
from app.models.summary import Classification, QualificationResult
from app.repositories.database import Database, dumps, now_iso, parse_iso, utcnow
from app.repositories.lead_repository import ConversationRepository, LeadRepository
from app.services.message_queue import Job, JobQueue, PermanentJobError
from app.services.notifier import Notifier
from app.services.outreach import JOB_NOTIFY
from app.services.spreadsheet_sync import SyncService
from app.services.workflow import can_transition

log = logging.getLogger(__name__)

JOB_PROCESS = "PROCESS_CONVERSATION"
JOB_CLOSE = "CLOSE_CONVERSATION"
JOB_SUMMARIZE = "SUMMARIZE_CONVERSATION"

STATUS_RANK = {"sending": 0, "sent": 1, "delivered": 2, "read": 3}


def _move(leads: LeadRepository, lead_id: str, target: LeadState, conn: sqlite3.Connection,
          reason: str | None = None) -> bool:
    """Transition only if allowed from the current state (no-op otherwise)."""
    row = leads.get(lead_id, conn)
    if row is None or not can_transition(row["status"], target):
        return False
    return leads.set_status(lead_id, target, reason=reason, conn=conn)


class ConversationManager:
    def __init__(self, settings: Settings, db: Database, queue: JobQueue, provider: MessagingProvider,
                 agent: SalesAgent, qualification: QualificationEngine, summarizer: Summarizer,
                 fallback_summarizer: Summarizer, sync: SyncService, notifier: Notifier) -> None:
        self.settings = settings
        self.db = db
        self.queue = queue
        self.provider = provider
        self.agent = agent
        self.qualification = qualification
        self.summarizer = summarizer
        self.fallback_summarizer = fallback_summarizer
        self.sync = sync
        self.notifier = notifier
        self.leads = LeadRepository(db)
        self.convs = ConversationRepository(db)

    # ================================================================ webhook
    def handle_webhook(self, parsed: ParsedWebhook) -> dict[str, int]:
        stats = {"messages": 0, "duplicates": 0, "statuses": 0, "unmatched": 0}
        for status in parsed.statuses:
            if self._record_status(status):
                stats["statuses"] += 1
        for message in parsed.messages:
            outcome = self.record_inbound(message)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _record_status(self, status: StatusUpdate) -> bool:
        with self.db.tx() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO status_events(event_key, wa_message_id, status, payload_json, "
                "received_at) VALUES (?, ?, ?, ?, ?)",
                (f"{status.wa_message_id}:{status.status}", status.wa_message_id, status.status,
                 dumps(status.raw), now_iso())).rowcount
            if not inserted:
                return False
            msg = conn.execute("SELECT * FROM messages WHERE wa_message_id=?",
                               (status.wa_message_id,)).fetchone()
            if msg is None or msg["direction"] != "outbound":
                return True  # unknown or not ours; recorded for audit only
            current = msg["status"] or "sending"
            if status.status == "failed":
                error = "; ".join(f"{e.get('code')}: {e.get('title') or e.get('message')}"
                                  for e in status.errors) or "delivery failed"
                conn.execute("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?",
                             (error[:500], now_iso(), msg["id"]))
                lead_id = msg["lead_id"]
                if msg["msg_type"] == "template" and lead_id:
                    _move(self.leads, lead_id, LeadState.MESSAGE_FAILED, conn, reason=error[:300])
                    self.leads.update(lead_id, conn=conn, last_error=error[:500], delivery_status="failed")
                    self.sync.request_sync(lead_id, conn=conn)
                self.queue.enqueue(JOB_NOTIFY, {"title": "WhatsApp delivery failed", "lead_id": lead_id,
                                                "detail": error[:300]},
                                   dedupe_key=f"notify:deliveryfail:{msg['id']}", conn=conn)
                return True
            if STATUS_RANK.get(status.status, -1) > STATUS_RANK.get(current, -1) and current != "failed":
                conn.execute("UPDATE messages SET status=?, updated_at=? WHERE id=?",
                             (status.status, now_iso(), msg["id"]))
                if msg["msg_type"] == "template" and msg["lead_id"]:
                    self.leads.update(msg["lead_id"], conn=conn, delivery_status=status.status)
                    if status.status in {"delivered", "read"}:
                        _move(self.leads, msg["lead_id"], LeadState.DELIVERED, conn)
        return True

    def _match_conversation(self, phone: str, conn: sqlite3.Connection) -> tuple[Any, Any]:
        """Most recent conversation for this phone, and its lead."""
        conv = conn.execute(
            "SELECT * FROM conversations WHERE wa_phone=? ORDER BY "
            "COALESCE(last_inbound_at, last_outbound_at, opened_at) DESC, conversation_id DESC LIMIT 1",
            (phone,)).fetchone()
        if conv is None:
            return None, None
        return conv, self.leads.get(conv["lead_id"], conn)

    def record_inbound(self, msg: InboundMessage) -> str:
        now = now_iso()
        notify: dict[str, Any] | None = None
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM messages WHERE wa_message_id=?",
                            (msg.wa_message_id,)).fetchone():
                return "duplicates"
            conv, lead = self._match_conversation(msg.from_phone, conn)
            meta = {"media_id": msg.media_id, "profile_name": msg.profile_name, "timestamp": msg.timestamp}
            if conv is None:
                self.convs.insert_message(conn, conversation_id=None, lead_id=None, direction="inbound",
                                          sender="customer", msg_type=msg.msg_type,
                                          body=msg.content_for_agent, wa_message_id=msg.wa_message_id,
                                          status="received", meta=meta)
                self.db.audit("inbound_unmatched", conn=conn, sender=mask_phone(msg.from_phone))
                self.queue.enqueue(JOB_NOTIFY, {"title": "Message from unknown WhatsApp number",
                                                "phone": mask_phone(msg.from_phone),
                                                "detail": msg.content_for_agent[:300]},
                                   dedupe_key=f"notify:unknown:{msg.wa_message_id}", conn=conn)
                return "unmatched"

            conv_id, lead_id = conv["conversation_id"], conv["lead_id"]
            msg_row_id = self.convs.insert_message(
                conn, conversation_id=conv_id, lead_id=lead_id, direction="inbound", sender="customer",
                msg_type=msg.msg_type, body=msg.content_for_agent, wa_message_id=msg.wa_message_id,
                status="received", meta=meta)
            self.convs.update(conv_id, conn=conn, last_inbound_at=now)
            self.leads.update(lead_id, conn=conn, last_message_at=now)

            if lead["status"] == LeadState.DO_NOT_CONTACT.value:
                self.db.audit("inbound_from_opted_out", lead_id=lead_id, conversation_id=conv_id, conn=conn)
                notify = {"title": "Opted-out customer sent a message", "lead_id": lead_id,
                          "detail": msg.content_for_agent[:300]}
            elif conv["automation_paused"] or conv["status"] == "HUMAN":
                notify = {"title": "New message in human-handled conversation", "lead_id": lead_id,
                          "detail": msg.content_for_agent[:300]}
            else:
                if conv["status"] == "CLOSED":
                    if not self.settings.reopen_on_inbound:
                        notify = {"title": "Message on closed conversation", "lead_id": lead_id,
                                  "detail": msg.content_for_agent[:300]}
                    else:
                        self.convs.update(conv_id, conn=conn, status="ACTIVE", closed_at=None,
                                          close_reason=None)
                        self.leads.update(lead_id, conn=conn, closed_at=None)
                        self.db.audit("conversation_reopened", lead_id=lead_id, conversation_id=conv_id,
                                      conn=conn)
                if notify is None:
                    _move(self.leads, lead_id, LeadState.CONVERSATION_ACTIVE, conn)
                    self.queue.enqueue(JOB_PROCESS, {"conversation_id": conv_id},
                                       dedupe_key=f"process:{msg.wa_message_id}",
                                       serial_key=f"conv:{conv_id}", conn=conn)
            if notify is not None:
                self.queue.enqueue(JOB_NOTIFY, notify, dedupe_key=f"notify:inbound:{msg.wa_message_id}",
                                   conn=conn)
            log.info("Inbound message %s stored for lead %s (conversation %s)",
                     msg_row_id, lead_id, conv_id)
        return "messages"

    # ================================================================ processing
    def _history(self, conv_id: int, upto_id: int | None = None) -> list[dict[str, str]]:
        history = []
        for m in self.convs.messages(conv_id):
            if upto_id is not None and m["id"] > upto_id:
                continue
            if m["direction"] == "outbound" and m["status"] in {"failed"}:
                continue
            role = "customer" if m["direction"] == "inbound" else ("human" if m["sender"] == "human" else "agent")
            history.append({"role": role, "text": m["body"] or ""})
        return history

    def handle_process(self, job: Job) -> None:
        conv_id = int(job.payload["conversation_id"])
        conv = self.convs.get(conv_id)
        if conv is None:
            raise PermanentJobError(f"conversation {conv_id} not found")

        pending_reply = self.db.query_one(
            "SELECT * FROM messages WHERE conversation_id=? AND direction='outbound' AND sender='agent' "
            "AND msg_type='text' AND status='sending' ORDER BY id LIMIT 1", (conv_id,))
        if pending_reply is not None:
            self._deliver(conv_id, pending_reply, job)
            return
        if conv["automation_paused"] or conv["status"] != "ACTIVE":
            return
        new = self.convs.unprocessed_inbound(conv_id, conv["last_processed_message_id"])
        if not new:
            return
        last_new_id = new[-1]["id"]
        lead = dict(self.leads.get(conv["lead_id"]))
        memory = self.convs.memory(conv)
        history = self._history(conv_id, upto_id=new[0]["id"] - 1)
        turn = AgentTurn(lead=lead, memory=memory, history=history,
                         new_messages=[m["body"] or "" for m in new])
        try:
            result = self.agent.respond(turn)
        except LLMError as exc:
            if job.attempts < job.max_attempts:
                raise
            result = None
            log.error("Agent failed permanently for conversation %s: %s", conv_id, exc)
        try:
            self.provider.mark_read(new[-1]["wa_message_id"])
        except Exception:  # noqa: BLE001 - read receipts are best effort
            pass

        reply_row = self._apply_result(conv_id, conv["last_processed_message_id"], last_new_id, result)
        if reply_row is not None:
            self._deliver(conv_id, reply_row, job)

    def _apply_result(self, conv_id: int, expected_processed: int, last_new_id: int,
                      result: AgentResult | None) -> Any:
        """Atomically claim the reply, update memory/state. Returns the reply row to send."""
        with self.db.tx() as conn:
            conv = self.convs.get(conv_id, conn)
            if conv["automation_paused"] or conv["status"] != "ACTIVE":
                self.db.audit("reply_discarded_paused", conversation_id=conv_id, conn=conn)
                return None
            if conv["last_processed_message_id"] != expected_processed:
                return None  # another job already answered these messages
            lead_id = conv["lead_id"]
            memory = self.convs.memory(conv)

            if result is None:  # LLM unavailable: safe fallback + human handover
                reply_text = self.agent.fallback_reply()
                memory.setdefault("handover", {}).update(
                    {"active": True, "reason": "AI assistant unavailable", "at": now_iso()})
                reply_id = self._insert_reply(conn, conv_id, lead_id, reply_text, {"fallback": True})
                self._handover(conn, conv, memory, "AI assistant unavailable")
                self.convs.update(conv_id, conn=conn, memory=memory, last_processed_message_id=last_new_id)
                return conn.execute("SELECT * FROM messages WHERE id=?", (reply_id,)).fetchone()

            decision = result.decision
            self._merge_memory(memory, result)
            qual = self.qualification.evaluate(memory, conv_lead_requirement(self.leads.get(lead_id, conn)))
            memory["qualification"] = qual.model_dump(mode="json")

            reply_id = self._insert_reply(conn, conv_id, lead_id, decision.reply_text, {
                "intent": decision.intent, "guard": decision.guard_notes,
                "quoted": decision.quoted_product_ids, "llm": result.llm_used})
            for check in result.quoted:
                p = check.product
                conn.execute(
                    "INSERT INTO quotes_audit(lead_id, conversation_id, message_id, product_id, model, "
                    "listed_price, currency, tax_included, tax_rate, source_file, source_reference, "
                    "price_valid_until, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (lead_id, conv_id, reply_id, p.product_id, p.model, p.listed_price, p.currency,
                     None if p.tax_included is None else int(p.tax_included), p.tax_rate,
                     p.source_file, p.source_reference, p.price_valid_until, now_iso()))
                self.db.audit("price_quoted", lead_id=lead_id, conversation_id=conv_id, conn=conn,
                              model=p.model, price=p.listed_price, currency=p.currency,
                              source=f"{p.source_file} {p.source_reference}")
            if decision.guard_notes:
                self.db.audit("reply_guard_triggered", lead_id=lead_id, conversation_id=conv_id,
                              conn=conn, violations=decision.guard_notes)
            if result.rules.injection_attempt:
                self.db.audit("prompt_injection_suspected", lead_id=lead_id, conversation_id=conv_id,
                              conn=conn)

            self.convs.update(conv_id, conn=conn, memory=memory, last_processed_message_id=last_new_id)

            if decision.intent == "opt_out":
                self.leads.set_status(lead_id, LeadState.DO_NOT_CONTACT, reason="customer opted out",
                                      conn=conn, force=True)
                self.leads.add_dnc(conv["wa_phone"], "customer opted out via WhatsApp", conn=conn)
                self.db.audit("do_not_contact", lead_id=lead_id, conversation_id=conv_id, conn=conn)
                self._begin_closure(conn, conv_id, "customer_opted_out", final_status="CLOSED")
            elif decision.handover_required:
                self._handover(conn, self.convs.get(conv_id, conn), memory,
                               decision.handover_reason or "Agent requested human assistance")
            elif decision.conversation_complete:
                _move(self.leads, lead_id, LeadState.AWAITING_CUSTOMER, conn)
                self.queue.enqueue(JOB_CLOSE, {"conversation_id": conv_id, "reason": "customer_finished"},
                                   dedupe_key=f"close:{conv_id}:{conv['closure_seq']}:finished", conn=conn)
            else:
                target = LeadState.QUALIFYING if decision.questions_asked and \
                    qual.classification not in {Classification.HOT} else LeadState.AWAITING_CUSTOMER
                _move(self.leads, lead_id, target, conn)
            return conn.execute("SELECT * FROM messages WHERE id=?", (reply_id,)).fetchone()

    def _insert_reply(self, conn: sqlite3.Connection, conv_id: int, lead_id: str, text: str,
                      meta: dict[str, Any]) -> int:
        reply_id = self.convs.insert_message(
            conn, conversation_id=conv_id, lead_id=lead_id, direction="outbound", sender="agent",
            msg_type="text", body=text, status="sending", meta=meta)
        assert reply_id is not None
        return reply_id

    def _merge_memory(self, memory: dict[str, Any], result: AgentResult) -> None:
        d = result.decision
        facts = memory.setdefault("facts", {})
        for key, value in d.facts.model_dump().items():
            if key == "objections":
                facts["objections"] = list(dict.fromkeys([*(facts.get("objections") or []), *value]))
            elif value not in (None, "", []):
                facts[key] = value
        known_products: dict[str, str] = {}
        for key in result.model_keys:
            products = self.agent.price_lookup.repo.by_model_key(key)
            if products:
                known_products[key] = products[0].model or key
        for name in d.products_discussed:  # accept model names only if they exist in the catalogue
            if (k := model_key(name)) and self.agent.price_lookup.repo.by_model_key(k):
                known_products.setdefault(k, self.agent.price_lookup.repo.by_model_key(k)[0].model or name)
        memory["product_keys"] = list(dict.fromkeys([*(memory.get("product_keys") or []), *known_products]))
        memory["products_of_interest"] = list(dict.fromkeys(
            [*(memory.get("products_of_interest") or []), *known_products.values()]))
        memory["questions_asked"] = list(dict.fromkeys([*(memory.get("questions_asked") or []),
                                                        *d.questions_asked]))
        memory["pending_questions"] = list(dict.fromkeys(
            [*(memory.get("pending_questions") or []), *d.unanswered_questions]))[-10:]
        prices = memory.setdefault("prices_shared", [])
        for check in result.quoted:
            p = check.product
            prices.append({"model": p.model, "configuration": p.configuration, "price": p.listed_price,
                           "currency": p.currency, "tax": p.tax_treatment(), "at": now_iso()})
        flags = memory.setdefault("flags", {})
        if any(c.status.value == "CONFLICT" for c in result.price_checks):
            flags["price_conflict"] = True
        if result.rules.complaint:
            flags["complaint"] = True
        if d.guard_notes:
            flags["guard_violations"] = int(flags.get("guard_violations", 0)) + 1
            if flags["guard_violations"] >= 2:
                flags["repeated_guard_violation"] = True
        if result.rules.injection_attempt:
            flags["injection_attempts"] = int(flags.get("injection_attempts", 0)) + 1
        if d.irrelevant:
            flags["irrelevant"] = True
        elif result.model_keys:
            flags["irrelevant"] = False
        follow = [label for key, label in (("wants_call", "Customer requested a call"),
                                           ("wants_demo", "Demo requested"),
                                           ("wants_site_visit", "Site visit requested"),
                                           ("wants_quote", "Formal quotation requested"))
                  if facts.get(key)]
        if follow:
            memory["follow_up"] = "; ".join(follow)

    def _handover(self, conn: sqlite3.Connection, conv: Any, memory: dict[str, Any], reason: str) -> None:
        conv_id, lead_id = conv["conversation_id"], conv["lead_id"]
        memory["handover"] = {"active": True, "reason": reason, "at": now_iso()}
        self.convs.update(conv_id, conn=conn, automation_paused=1, status="HUMAN",
                          handover_reason=reason, memory=memory)
        _move(self.leads, lead_id, LeadState.HUMAN_HANDOVER, conn, reason=reason)
        self.db.audit("human_handover", lead_id=lead_id, conversation_id=conv_id, conn=conn, reason=reason)
        lead = self.leads.get(lead_id, conn)
        self.queue.enqueue(JOB_NOTIFY, {
            "title": "Human handover requested", "lead_id": lead_id,
            "customer": lead["customer_name"], "company": lead["company_name"],
            "phone": mask_phone(lead["mobile_e164"]), "detail": reason,
        }, dedupe_key=f"notify:handover:{conv_id}:{conv['closure_seq']}", conn=conn)
        # A handover is a closure event for the bot: record a summary for the salesperson.
        self._begin_closure(conn, conv_id, "human_handover", final_status="HUMAN")

    def _deliver(self, conv_id: int, reply: Any, job: Job) -> None:
        conv = self.convs.get(conv_id)
        last_inbound = parse_iso(conv["last_inbound_at"])
        window = timedelta(hours=self.settings.customer_service_window_hours)
        if last_inbound is None or utcnow() - last_inbound > window:
            self._fail_reply(conv, reply, "24-hour customer service window has expired; "
                                          "a template message is required")
            return
        try:
            result = self.provider.send_text(conv["wa_phone"], reply["body"])
        except WhatsAppError as exc:
            if exc.window_expired:
                self._fail_reply(conv, reply, str(exc))
                return
            if exc.retryable and job.attempts < job.max_attempts:
                raise
            self._fail_reply(conv, reply, str(exc))
            raise PermanentJobError(str(exc)) from exc
        now = now_iso()
        with self.db.tx() as conn:
            conn.execute("UPDATE messages SET status='sent', wa_message_id=?, updated_at=? WHERE id=?",
                         (result.wa_message_id, now, reply["id"]))
            self.convs.update(conv_id, conn=conn, last_outbound_at=now)
            self.leads.update(conv["lead_id"], conn=conn, last_message_at=now)

    def _fail_reply(self, conv: Any, reply: Any, error: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?",
                         (error[:500], now_iso(), reply["id"]))
            self.leads.update(conv["lead_id"], conn=conn, last_error=error[:500])
            self.db.audit("reply_failed", lead_id=conv["lead_id"], conversation_id=conv["conversation_id"],
                          conn=conn, error=error[:300])
            self.queue.enqueue(JOB_NOTIFY, {"title": "Reply could not be delivered",
                                            "lead_id": conv["lead_id"], "detail": error[:300]},
                               dedupe_key=f"notify:replyfail:{reply['id']}", conn=conn)
            self.sync.request_sync(conv["lead_id"], conn=conn)

    # ================================================================ closure
    def _begin_closure(self, conn: sqlite3.Connection, conv_id: int, reason: str,
                       final_status: str = "CLOSED") -> int:
        conv = self.convs.get(conv_id, conn)
        seq = conv["closure_seq"] + 1
        now = now_iso()
        fields: dict[str, Any] = {"closure_seq": seq, "close_reason": reason, "status": final_status}
        if final_status == "CLOSED":
            fields["closed_at"] = now
        self.convs.update(conv_id, conn=conn, **fields)
        lead = self.leads.get(conv["lead_id"], conn)
        if final_status == "CLOSED" and lead["status"] not in {LeadState.DO_NOT_CONTACT.value,
                                                                LeadState.HUMAN_HANDOVER.value}:
            _move(self.leads, conv["lead_id"], LeadState.SUMMARY_PENDING, conn, reason=reason)
        if final_status == "CLOSED":
            self.leads.update(conv["lead_id"], conn=conn, closed_at=now)
        self.db.audit("closure_event", lead_id=conv["lead_id"], conversation_id=conv_id, conn=conn,
                      reason=reason, seq=seq)
        self.queue.enqueue(JOB_SUMMARIZE, {"conversation_id": conv_id, "closure_seq": seq},
                           dedupe_key=f"summary:{conv_id}:{seq}", conn=conn)
        return seq

    def request_close(self, conv_id: int, reason: str) -> None:
        conv = self.convs.get(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        self.queue.enqueue(JOB_CLOSE, {"conversation_id": conv_id, "reason": reason},
                           dedupe_key=f"close:{conv_id}:{conv['closure_seq']}:{reason}")

    def handle_close(self, job: Job) -> None:
        conv_id, reason = int(job.payload["conversation_id"]), job.payload.get("reason", "closed")
        with self.db.tx() as conn:
            conv = self.convs.get(conv_id, conn)
            if conv is None or conv["status"] == "CLOSED":
                return
            if conv["status"] == "HUMAN" and reason not in {"operator_closed"}:
                return  # humans own handed-over conversations
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND direction='inbound' "
                "AND id>?", (conv_id, conv["last_processed_message_id"])).fetchone()["n"]
            if pending and reason in {"inactivity", "no_response"}:
                return  # customer wrote meanwhile; it will be processed first
            if reason == "operator_closed":
                memory = self.convs.memory(conv)
                if memory.get("handover"):
                    memory["handover"]["active"] = False
                self.convs.update(conv_id, conn=conn, automation_paused=0, memory=memory)
                lead = self.leads.get(conv["lead_id"], conn)
                if lead["status"] == LeadState.HUMAN_HANDOVER.value:
                    self.leads.set_status(conv["lead_id"], LeadState.SUMMARY_PENDING,
                                          reason="closed by operator", conn=conn)
            self._begin_closure(conn, conv_id, reason, final_status="CLOSED")

    def handle_summarize(self, job: Job) -> None:
        conv_id, seq = int(job.payload["conversation_id"]), int(job.payload["closure_seq"])
        existing = self.db.query_one("SELECT id FROM summaries WHERE conversation_id=? AND closure_seq=?",
                                     (conv_id, seq))
        conv = self.convs.get(conv_id)
        lead = self.leads.get(conv["lead_id"])
        if existing is None:
            memory = self.convs.memory(conv)
            qual = QualificationResult(**memory["qualification"]) if memory.get("qualification") else \
                self.qualification.evaluate(memory, conv_lead_requirement(lead))
            quotes = [dict(r) for r in self.db.query(
                "SELECT * FROM quotes_audit WHERE conversation_id=? ORDER BY id", (conv_id,))]
            kwargs = dict(lead=dict(lead), conversation=dict(conv), transcript=self._history(conv_id),
                          memory=memory, qualification=qual, quotes=quotes,
                          close_reason=conv["close_reason"] or "closed",
                          closed_at=conv["closed_at"] or now_iso())
            try:
                summary = self.summarizer.summarize(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if job.attempts < job.max_attempts:
                    raise
                log.error("Summary LLM failed permanently (%s); using rule-based summary", exc)
                summary = self.fallback_summarizer.summarize(**kwargs)
            with self.db.tx() as conn:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO summaries(conversation_id, lead_id, closure_seq, summary_json, "
                    "summary_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (conv_id, conv["lead_id"], seq, dumps(summary.model_dump_public()), summary.to_text(),
                     now_iso())).rowcount
                if inserted:
                    self.db.audit("summary_created", lead_id=conv["lead_id"], conversation_id=conv_id,
                                  conn=conn, seq=seq, classification=summary.lead_classification)
        with self.db.tx() as conn:
            _move(self.leads, conv["lead_id"], LeadState.SHEET_SYNC_PENDING, conn)
            self.sync.request_sync(conv["lead_id"], conn=conn)

    def on_all_synced(self, lead_id: str) -> None:
        """All destinations hold the summary: finish the workflow and write the final status."""
        with self.db.tx() as conn:
            lead = self.leads.get(lead_id, conn)
            if lead and lead["status"] == LeadState.SHEET_SYNC_PENDING.value:
                self.leads.set_status(lead_id, LeadState.CLOSED, conn=conn)
                self.sync.request_sync(lead_id, conn=conn)  # one more write for agent_status=CLOSED

    def check_inactivity(self) -> int:
        now = utcnow()
        inactive_cutoff = now - timedelta(minutes=self.settings.inactivity_close_minutes)
        no_reply_cutoff = now - timedelta(hours=self.settings.no_response_close_hours)
        enqueued = 0
        for conv in self.db.query("SELECT * FROM conversations WHERE status='ACTIVE' AND automation_paused=0"):
            last_in = parse_iso(conv["last_inbound_at"])
            last_out = parse_iso(conv["last_outbound_at"])
            if last_in is None:
                opened = parse_iso(conv["opened_at"])
                if last_out is not None and opened and opened < no_reply_cutoff:
                    reason = "no_response"
                else:
                    continue
            else:
                last = max(d for d in (last_in, last_out) if d is not None)
                if last >= inactive_cutoff:
                    continue
                reason = "inactivity"
            if self.queue.enqueue(JOB_CLOSE, {"conversation_id": conv["conversation_id"], "reason": reason},
                                  dedupe_key=f"close:{conv['conversation_id']}:{conv['closure_seq']}:{reason}"):
                enqueued += 1
        return enqueued

    # ================================================================ human operators
    def take_over(self, conv_id: int, operator: str, reason: str = "Operator took over") -> None:
        with self.db.tx() as conn:
            conv = self.convs.get(conv_id, conn)
            if conv is None:
                raise KeyError(conv_id)
            if conv["automation_paused"]:
                return
            memory = self.convs.memory(conv)
            self.leads.update(conv["lead_id"], conn=conn, assigned_to=operator)
            self._handover(conn, conv, memory, f"{reason} ({operator})")

    def resume_automation(self, conv_id: int, operator: str) -> None:
        with self.db.tx() as conn:
            conv = self.convs.get(conv_id, conn)
            if conv is None:
                raise KeyError(conv_id)
            memory = self.convs.memory(conv)
            if memory.get("handover"):
                memory["handover"]["active"] = False
                memory["handover"]["resolved_by"] = operator
            # Messages that arrived while a human was in charge are considered handled.
            last = conn.execute("SELECT MAX(id) AS m FROM messages WHERE conversation_id=?",
                                (conv_id,)).fetchone()["m"] or 0
            self.convs.update(conv_id, conn=conn, automation_paused=0, status="ACTIVE",
                              handover_reason=None, memory=memory, last_processed_message_id=last)
            lead = self.leads.get(conv["lead_id"], conn)
            if lead["status"] in {LeadState.HUMAN_HANDOVER.value, LeadState.SHEET_SYNC_PENDING.value,
                                  LeadState.SUMMARY_PENDING.value, LeadState.CLOSED.value}:
                self.leads.set_status(conv["lead_id"], LeadState.CONVERSATION_ACTIVE,
                                      reason=f"automation resumed by {operator}", conn=conn, force=True)
            self.db.audit("automation_resumed", lead_id=conv["lead_id"], conversation_id=conv_id,
                          conn=conn, operator=operator)
            self.sync.request_sync(conv["lead_id"], conn=conn)

    def human_send(self, conv_id: int, operator: str, text: str) -> str:
        conv = self.convs.get(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        if not conv["automation_paused"]:
            raise ValueError("take over the conversation before sending as a human")
        last_inbound = parse_iso(conv["last_inbound_at"])
        if last_inbound is None or utcnow() - last_inbound > timedelta(
                hours=self.settings.customer_service_window_hours):
            raise ValueError("outside the 24-hour window: send an approved template instead")
        with self.db.tx() as conn:
            msg_id = self.convs.insert_message(
                conn, conversation_id=conv_id, lead_id=conv["lead_id"], direction="outbound",
                sender="human", msg_type="text", body=text, status="sending", meta={"operator": operator})
        result = self.provider.send_text(conv["wa_phone"], text)
        with self.db.tx() as conn:
            conn.execute("UPDATE messages SET status='sent', wa_message_id=?, updated_at=? WHERE id=?",
                         (result.wa_message_id, now_iso(), msg_id))
            self.convs.update(conv_id, conn=conn, last_outbound_at=now_iso())
            self.db.audit("human_message_sent", lead_id=conv["lead_id"], conversation_id=conv_id,
                          conn=conn, operator=operator)
        return result.wa_message_id

    def handle_notify(self, job: Job) -> None:
        payload = dict(job.payload)
        title = payload.pop("title", "Notification")
        self.notifier.notify(title, payload)

    # ================================================================ retention
    def purge_old_data(self) -> int:
        if self.settings.retention_days <= 0:
            return 0
        cutoff = (utcnow() - timedelta(days=self.settings.retention_days)).isoformat()
        with self.db.tx() as conn:
            n = conn.execute(
                "DELETE FROM messages WHERE conversation_id IN (SELECT conversation_id FROM conversations "
                "WHERE status='CLOSED' AND closed_at < ?)", (cutoff,)).rowcount
            conn.execute("DELETE FROM outbox_mock WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM status_events WHERE received_at < ?", (cutoff,))
        if n:
            log.info("Retention: purged %d message(s) older than %d days", n, self.settings.retention_days)
        return n


def conv_lead_requirement(lead: Any) -> str | None:
    return lead["requirement"] if lead is not None else None
