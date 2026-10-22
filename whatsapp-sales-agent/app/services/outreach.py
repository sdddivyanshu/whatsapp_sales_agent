"""CONTACT_LEAD job: validate a new lead and send the approved introduction template.

Send idempotency: the outbound message row is written (status ``sending``)
*before* calling the provider. A retried job re-sends that same row instead of
creating a new one, and a lead that already has a first contact is never
contacted again by this workflow.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from app.config import BusinessProfile, Settings
from app.ingestion.lead_validator import validate_lead
from app.integrations.whatsapp_cloud import MessagingProvider, WhatsAppError
from app.logging_utils import mask_phone
from app.models.lead import LeadInput, LeadState
from app.repositories.database import Database, loads, now_iso, utcnow
from app.repositories.lead_repository import ConversationRepository, LeadRepository
from app.services.message_queue import Job, JobQueue, PermanentJobError
from app.services.spreadsheet_sync import SyncService
from app.services.workflow import can_transition

log = logging.getLogger(__name__)

JOB_NOTIFY = "NOTIFY"


def _param(value: str | None, fallback: str, limit: int = 60) -> str:
    """Template parameters may not contain newlines/tabs or runs of spaces."""
    text = re.sub(r"\s+", " ", (value or "").strip())
    if not text:
        text = fallback
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class OutreachService:
    def __init__(self, settings: Settings, db: Database, queue: JobQueue,
                 provider: MessagingProvider, profile: BusinessProfile, sync: SyncService) -> None:
        self.settings = settings
        self.db = db
        self.queue = queue
        self.provider = provider
        self.profile = profile
        self.sync = sync
        self.leads = LeadRepository(db)
        self.convs = ConversationRepository(db)

    # ---------------------------------------------------------------- templates
    def template_values(self, lead: Any) -> dict[str, str]:
        neutral = self.profile.known("outreach.neutral_greeting_name") or "Sir/Madam"
        return {
            "customer_name": _param(lead["customer_name"], neutral),
            "company_name": _param(lead["company_name"], "your company"),
            "requirement": _param(lead["requirement"], "industrial machinery"),
            "our_company": _param(self.profile.company_name, "our company"),
            "sales_phone": _param(str(self.profile.known("contact.sales_phone") or ""), "-"),
        }

    def render_intro(self, values: dict[str, str]) -> str:
        preview = self.profile.known("outreach.template_preview") or (
            "Hello {customer_name}, this is {our_company}. Thank you for your enquiry about "
            "{requirement}. I'm here to help with product details and pricing. Could you tell me "
            "a little about your application?")
        try:
            return preview.format(**values)
        except (KeyError, IndexError):
            return preview

    # ---------------------------------------------------------------- job
    def handle_contact(self, job: Job) -> None:
        lead_id = job.payload["lead_id"]
        lead = self.leads.get(lead_id)
        if lead is None:
            raise PermanentJobError(f"lead {lead_id} not found")

        pending = self.db.query_one(
            "SELECT * FROM messages WHERE lead_id=? AND direction='outbound' AND msg_type='template' "
            "AND status='sending' ORDER BY id DESC LIMIT 1", (lead_id,))
        if pending is None:
            if lead["first_contact_at"] or self.convs.latest_for_lead(lead_id) is not None:
                self.db.audit("contact_skipped_already_contacted", lead_id=lead_id)
                return
            if not self._validate(lead_id, job):
                return
            pending = self._prepare_send(lead_id)
            if pending is None:
                return
        self._send(lead_id, pending, job)

    def _validate(self, lead_id: str, job: Job) -> bool:
        with self.db.tx() as conn:
            lead = self.leads.get(lead_id, conn)
            current = LeadState(lead["status"])
            if current == LeadState.READY_TO_CONTACT:
                return True  # validated before an interruption
            if not can_transition(current, LeadState.VALIDATING):
                self.db.audit("contact_skipped_state", lead_id=lead_id, conn=conn, state=current.value)
                return False
            self.leads.set_status(lead_id, LeadState.VALIDATING, conn=conn)
            raw = loads(lead["raw_json"], {})
            lead_input = LeadInput(**{k: str(raw.get(k, "") or "") for k in LeadInput.model_fields
                                      if k != "extra"})
            e164_guess = lead["mobile_e164"]
            outcome = validate_lead(
                lead_input, default_country_code=self.settings.default_country_code,
                consent_required=self.settings.consent_required,
                consent_accepted_values=self.settings.consent_accepted_values,
                is_dnc=bool(e164_guess) and self.leads.is_dnc(e164_guess, conn))
            if not outcome.valid:
                reason = "; ".join(outcome.reasons)
                self.leads.set_status(lead_id, outcome.state, reason=reason, conn=conn)
                self.leads.update(lead_id, conn=conn, last_error=reason if outcome.state ==
                                  LeadState.INVALID_LEAD else None)
                self.sync.request_sync(lead_id, conn=conn)
                log.info("Lead %s not contacted: %s", lead_id, reason)
                return False

            policy = self.settings.duplicate_policy
            if policy != "off" and not job.payload.get("skip_duplicate_check"):
                other = self.leads.find_by_identity(lead["identity_hash"], conn, exclude=lead_id)
                if other is not None:
                    created = other["created_at"]
                    cutoff = (utcnow() - timedelta(days=self.settings.duplicate_window_days)).isoformat()
                    if created >= cutoff:
                        self.leads.update(lead_id, conn=conn, duplicate_of=other["lead_id"])
                        self.db.audit("duplicate_detected", lead_id=lead_id, conn=conn,
                                      duplicate_of=other["lead_id"], policy=policy)
                        if policy == "review":
                            self.leads.set_status(lead_id, LeadState.DUPLICATE_REVIEW,
                                                  reason=f"same phone and requirement as {other['lead_id']}",
                                                  conn=conn)
                            self.sync.request_sync(lead_id, conn=conn)
                            return False
            self.leads.update(lead_id, conn=conn, mobile_e164=outcome.mobile_e164)
            self.leads.set_status(lead_id, LeadState.READY_TO_CONTACT, conn=conn)
        return True

    def _prepare_send(self, lead_id: str) -> Any:
        with self.db.tx() as conn:
            lead = self.leads.get(lead_id, conn)
            if lead["status"] != LeadState.READY_TO_CONTACT.value:
                return None
            values = self.template_values(lead)
            params = [values.get(name, "") for name in self.settings.whatsapp_template_params]
            conv_id = self.convs.create(conn, lead_id, lead["mobile_e164"])
            msg_id = self.convs.insert_message(
                conn, conversation_id=conv_id, lead_id=lead_id, direction="outbound", sender="agent",
                msg_type="template", body=self.render_intro(values), status="sending",
                meta={"template": self.settings.whatsapp_template_name,
                      "language": self.settings.whatsapp_template_language, "params": params})
            # The intro is context for the agent but needs no processing itself.
            self.convs.update(conv_id, conn=conn, last_processed_message_id=msg_id or 0)
        return self.db.query_one("SELECT * FROM messages WHERE id=?", (msg_id,))

    def _send(self, lead_id: str, message: Any, job: Job) -> None:
        lead = self.leads.get(lead_id)
        meta = loads(message["meta_json"], {})
        to = lead["mobile_e164"]
        try:
            result = self.provider.send_template(to, meta["template"], meta["language"], meta["params"])
        except WhatsAppError as exc:
            final = (not exc.retryable) or job.attempts >= job.max_attempts
            if not final:
                raise  # job queue retries with backoff, reusing this message row
            with self.db.tx() as conn:
                conn.execute("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?",
                             (str(exc)[:500], now_iso(), message["id"]))
                self.leads.set_status(lead_id, LeadState.MESSAGE_FAILED, reason=str(exc)[:300], conn=conn)
                self.leads.update(lead_id, conn=conn, last_error=str(exc)[:500])
                self.sync.request_sync(lead_id, conn=conn)
                self.queue.enqueue(JOB_NOTIFY, {"title": "Intro message failed", "lead_id": lead_id,
                                                "detail": str(exc)[:300]},
                                   dedupe_key=f"notify:introfail:{message['id']}", conn=conn)
            raise PermanentJobError(str(exc)) from exc

        now = now_iso()
        with self.db.tx() as conn:
            conn.execute("UPDATE messages SET status='sent', wa_message_id=?, updated_at=? WHERE id=?",
                         (result.wa_message_id, now, message["id"]))
            self.leads.set_status(lead_id, LeadState.MESSAGE_SENT, conn=conn)
            self.leads.update(lead_id, conn=conn, first_contact_at=now, last_message_at=now,
                              delivery_status="sent", last_error=None)
            self.convs.update(message["conversation_id"], conn=conn, last_outbound_at=now)
            self.db.audit("intro_sent", lead_id=lead_id, conn=conn, wa_message_id=result.wa_message_id,
                          template=meta["template"], to=mask_phone(to))
            self.sync.request_sync(lead_id, conn=conn)
        log.info("Intro template sent to lead %s (%s)", lead_id, mask_phone(to))

    def retry_contact(self, lead_id: str, approve_duplicate: bool = False) -> None:
        """Operator action: retry a failed intro, or re-validate a fixed / approved lead."""
        with self.db.tx() as conn:
            lead = self.leads.get(lead_id, conn)
            if lead is None:
                raise KeyError(lead_id)
            status = LeadState(lead["status"])
            if status == LeadState.MESSAGE_FAILED:
                conn.execute("UPDATE messages SET status='sending', error=NULL, updated_at=? WHERE "
                             "lead_id=? AND msg_type='template' AND status='failed'", (now_iso(), lead_id))
                self.leads.set_status(lead_id, LeadState.READY_TO_CONTACT, reason="operator retry", conn=conn)
            elif not can_transition(status, LeadState.VALIDATING):
                raise ValueError(f"lead is {status.value}; nothing to retry")
            self.queue.enqueue("CONTACT_LEAD", {"lead_id": lead_id, "operator_retry": True,
                                                "skip_duplicate_check": approve_duplicate},
                               dedupe_key=f"contact:{lead_id}:retry:{now_iso()}", conn=conn)
            self.db.audit("operator_retry_contact", lead_id=lead_id, conn=conn)
