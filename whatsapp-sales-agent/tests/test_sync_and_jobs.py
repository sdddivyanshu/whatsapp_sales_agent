"""Spreadsheet sync retries, failure handling, restart recovery and the state machine."""

from __future__ import annotations

import pytest
from conftest import inbound, outbound_bodies, read_csv, start_lead

from app.integrations.google_sheets import GoogleSheetsSink, InMemorySheetsBackend
from app.integrations.whatsapp_cloud import WhatsAppError
from app.models.lead import LeadState
from app.services.message_queue import JobQueue, PermanentJobError, Worker
from app.services.workflow import InvalidTransition, assert_transition, can_transition

PHONE = "+919876500001"


def test_sheets_sync_retries_without_duplicate_rows(make_app, settings):
    settings.output_sinks = ["csv", "sheets"]
    backend = InMemorySheetsBackend([["lead_id", "customer_name", "mobile_number"],
                                     ["L100", "Rahul Sharma", "+919876500001"]])
    app = make_app(sheets_backend=backend)
    backend.fail_next = 2                             # Sheets API is down for two calls
    start_lead(app)
    state = {r["sink"]: r for r in app.db.query("SELECT * FROM sync_status WHERE lead_id='L100'")}
    assert state["csv"]["status"] == "SYNCED"         # destinations are tracked independently
    # Retries are scheduled with backoff (0s in tests); drain until done.
    for _ in range(5):
        app.worker.drain()
    state = {r["sink"]: r for r in app.db.query("SELECT * FROM sync_status WHERE lead_id='L100'")}
    assert state["sheets"]["status"] == "SYNCED"
    inbound(app, PHONE, "Thanks, that's all")
    for _ in range(3):
        app.worker.drain()
    rows = backend.values
    assert [r[0] for r in rows].count("L100") == 1   # updated in place, never appended twice
    assert backend.append_calls == 0
    header = rows[0]
    assert rows[1][header.index("agent_status")] == "CLOSED"
    assert rows[1][header.index("customer_name")] == "Rahul Sharma"


def test_sheets_sink_appends_missing_row_once():
    backend = InMemorySheetsBackend([["lead_id", "customer_name"]])
    sink = GoogleSheetsSink(backend)
    assert sink.upsert("X1", {"customer_name": "A"}, {"agent_status": "MESSAGE_SENT"}) == "appended"
    assert sink.upsert("X1", {"customer_name": "A"}, {"agent_status": "CLOSED"}) == "updated"
    assert len(backend.values) == 2 and backend.values[1][-1] == "CLOSED"


def test_permanent_template_failure_is_handled_safely(app):
    app.provider.fail_next.append(WhatsAppError("WhatsApp API error 132001: template does not exist",
                                                code=132001, retryable=False))
    start_lead(app)
    lead = app.db.query_one("SELECT * FROM leads WHERE lead_id='L100'")
    assert lead["status"] == LeadState.MESSAGE_FAILED.value and "132001" in lead["last_error"]
    job = app.db.query_one("SELECT * FROM jobs WHERE job_type='CONTACT_LEAD'")
    assert job["status"] == "DEAD"
    row = read_csv(app.settings.leads_csv_path)[0]
    assert row["agent_status"] == "MESSAGE_FAILED" and "132001" in row["last_error"]
    assert any(n["title"] == "Intro message failed" for n in app.notifier.sent)
    # Operator fixes the template and retries: the same message row is reused.
    app.outreach.retry_contact("L100")
    app.worker.drain()
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='L100'")["status"] == "MESSAGE_SENT"
    assert app.db.query_one("SELECT COUNT(*) AS n FROM messages WHERE msg_type='template'")["n"] == 1


def test_transient_failure_retries_and_sends_once(app):
    app.provider.fail_next.append(WhatsAppError("HTTP 503", retryable=True, status_code=503))
    start_lead(app)
    app.worker.drain()
    templates = [m for m in app.provider.sent if m["kind"] == "template"]
    assert len(templates) == 1
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='L100'")["status"] == "MESSAGE_SENT"


def test_expired_service_window_is_not_bypassed(app):
    start_lead(app)
    app.provider.fail_next.append(WhatsAppError("WhatsApp API error 131047: re-engagement message",
                                                code=131047, window_expired=True))
    inbound(app, PHONE, "Price of WC-400?")
    failed = app.db.query_one("SELECT * FROM messages WHERE direction='outbound' AND msg_type='text'")
    assert failed["status"] == "failed" and "131047" in failed["error"]
    assert any(n["title"] == "Reply could not be delivered" for n in app.notifier.sent)
    assert [m for m in app.provider.sent if m["kind"] == "text"] == []   # no free-form resend


def test_delivery_failure_status_marks_lead(app):
    from app.models.message import ParsedWebhook, StatusUpdate

    start_lead(app)
    wamid = app.db.query_one("SELECT wa_message_id FROM messages WHERE lead_id='L100'")["wa_message_id"]
    app.conversations.handle_webhook(ParsedWebhook(statuses=[StatusUpdate(
        wa_message_id=wamid, status="failed", errors=[{"code": 131026, "title": "Message undeliverable"}])]))
    app.worker.drain()
    lead = app.db.query_one("SELECT * FROM leads WHERE lead_id='L100'")
    assert lead["status"] == "MESSAGE_FAILED" and "131026" in lead["last_error"]


def test_restart_recovers_pending_and_interrupted_jobs(make_app, settings):
    app = make_app()
    start_lead(app, lead_id="R0", phone="+919876500009")
    # Simulate a crash: a contact job claimed (RUNNING) and another still PENDING.
    from conftest import append_csv
    append_csv(settings.leads_csv_path, [["R1", "A", "B", "+919876500010", "EDM Drill", "OPTED_IN"],
                                         ["R2", "C", "D", "+919876500011", "EDM Drill", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    claimed = app.queue.claim_next()
    assert claimed is not None                        # "crashes" before completing

    restarted = make_app()
    recovered = restarted.queue.recover_stale(force_all=True)
    assert recovered == 1
    restarted.worker.drain()
    for lead_id in ("R1", "R2"):
        assert restarted.db.query_one("SELECT status FROM leads WHERE lead_id=?",
                                      (lead_id,))["status"] == "MESSAGE_SENT"
    assert len([m for m in restarted.provider.sent if m["kind"] == "template"]) == 2


def test_interrupted_reply_is_resent_not_regenerated(app):
    start_lead(app)
    app.provider.fail_next.append(WhatsAppError("timeout", retryable=True))
    inbound(app, PHONE, "Price of DR-20?", drain=False)
    app.worker.run_once()                             # LLM ran, reply claimed, send failed
    calls_before = len(app.llm.calls)
    app.worker.drain()                                # retry re-sends the stored reply
    assert len(app.llm.calls) == calls_before
    texts = [m for m in app.provider.sent if m["kind"] == "text"]
    assert len(texts) == 1 and "3,85,000" in texts[0]["body"]
    assert len(outbound_bodies(app, "L100")) == 2     # intro + one reply


def test_dead_letter_and_operator_retry(app):
    calls = {"n": 0}

    def flaky(job):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")

    queue = JobQueue(app.db, max_attempts=2, backoff_base=0)
    worker = Worker(queue, {"FLAKY": flaky})
    queue.enqueue("FLAKY", {}, dedupe_key="f1")
    worker.drain()
    assert queue.stats() == {"DEAD": 1}
    assert queue.retry_dead() == 1
    worker.drain()
    assert queue.stats() == {"DONE": 1}

    def permanent(job):
        raise PermanentJobError("bad data")

    worker2 = Worker(queue, {"PERM": permanent})
    queue.enqueue("PERM", {})
    worker2.drain()
    assert app.db.query_one("SELECT status, attempts FROM jobs WHERE job_type='PERM'")["status"] == "DEAD"


def test_enqueue_is_idempotent_with_dedupe_key(app):
    q = app.queue
    assert q.enqueue("X", {}, dedupe_key="same") is not None
    assert q.enqueue("X", {}, dedupe_key="same") is None


def test_state_machine_transitions():
    S = LeadState
    assert can_transition(S.NEW_LEAD, S.VALIDATING)
    assert can_transition(S.CLOSED, S.CONVERSATION_ACTIVE)          # reopen
    assert not can_transition(S.DO_NOT_CONTACT, S.MESSAGE_SENT)     # DNC is sticky
    assert not can_transition(S.NEW_LEAD, S.MESSAGE_SENT)           # cannot skip validation
    with pytest.raises(InvalidTransition):
        assert_transition(S.INVALID_LEAD, S.READY_TO_CONTACT)


def test_csv_sync_failure_is_retained_and_retried(app):
    start_lead(app)
    path = app.settings.leads_csv_path
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "BROKEN,ROW,WITH,TOO,MANY,FIELDS,X,Y,Z,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16\n",
                    encoding="utf-8")
    inbound(app, PHONE, "Thanks, that's all")
    state = app.db.query_one("SELECT * FROM sync_status WHERE lead_id='L100' AND sink='csv'")
    assert state["status"] == "FAILED" and "refusing to rewrite" in state["last_error"]
    assert app.db.query_one("SELECT COUNT(*) AS n FROM summaries")["n"] == 1   # durable in SQLite
    path.write_text(original, encoding="utf-8")       # operator fixes the file
    app.queue.retry_dead()
    for _ in range(3):
        app.worker.drain()
    assert read_csv(path)[0]["agent_status"] == "CLOSED"
