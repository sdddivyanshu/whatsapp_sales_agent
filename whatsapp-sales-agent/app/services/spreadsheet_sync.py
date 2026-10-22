"""Write-back of lead results to the CSV and/or Google Sheets.

SQLite holds the durable result. ``request_sync`` bumps a per-destination
``desired_version`` and enqueues one job per destination; each destination
tracks its own ``synced_version`` and retries independently. Writes are
idempotent (the row is located by ``lead_id`` and updated in place), so a
retried job can never create a duplicate row.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

from app.ingestion.csv_file import CsvDocument, CsvInvalid, locked_update
from app.ingestion.csv_monitor import assign_row_identities
from app.integrations.google_sheets import GoogleSheetsSink
from app.models.lead import INPUT_COLUMNS, OUTPUT_COLUMNS, LeadState
from app.models.product import format_amount
from app.repositories.database import Database, loads, now_iso
from app.services.message_queue import Job, JobQueue, PermanentJobError

log = logging.getLogger(__name__)

JOB_SYNC_OUTPUT = "SYNC_OUTPUT"

_STATE_ACTIONS = {
    LeadState.INVALID_LEAD.value: "Fix lead data",
    LeadState.CONSENT_REQUIRED.value: "Obtain WhatsApp opt-in before contacting",
    LeadState.DUPLICATE_REVIEW.value: "Review possible duplicate lead",
    LeadState.MESSAGE_FAILED.value: "Check number / template and retry",
    LeadState.DO_NOT_CONTACT.value: "Do not contact",
    LeadState.MESSAGE_SENT.value: "Awaiting customer reply",
    LeadState.DELIVERED.value: "Awaiting customer reply",
}


def build_output_values(db: Database, lead_id: str) -> dict[str, str]:
    lead = db.query_one("SELECT * FROM leads WHERE lead_id=?", (lead_id,))
    if lead is None:
        raise PermanentJobError(f"lead {lead_id} not found")
    conv = db.query_one("SELECT * FROM conversations WHERE lead_id=? ORDER BY conversation_id DESC "
                        "LIMIT 1", (lead_id,))
    memory = loads(conv["memory_json"], {}) if conv else {}
    summary = db.query_one("SELECT * FROM summaries WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead_id,))
    summary_json = loads(summary["summary_json"], {}) if summary else {}
    quotes = db.query("SELECT model, product_id, listed_price, currency, tax_included, tax_rate, "
                      "source_file, source_reference FROM quotes_audit WHERE lead_id=? ORDER BY id",
                      (lead_id,))
    seen: set[str] = set()
    quoted, sources = [], []
    for q in quotes:
        key = f"{q['model']}|{q['listed_price']}"
        if key in seen:
            continue
        seen.add(key)
        tax = {1: "GST incl.", 0: "+ GST"}.get(q["tax_included"], "GST tbc")
        if q["tax_rate"] is not None and q["tax_included"] is not None:
            tax += f" {q['tax_rate']:g}%"
        quoted.append(f"{q['model'] or q['product_id']}: {format_amount(q['listed_price'], q['currency'])} ({tax})")
        sources.append(f"{q['source_file']} [{q['source_reference']}]")

    qualification = memory.get("qualification") or {}
    facts = memory.get("facts") or {}
    handover = memory.get("handover") or {}
    status = lead["status"]
    next_action = summary_json.get("recommended_next_action")
    if status in _STATE_ACTIONS and (not next_action or status in {
            LeadState.INVALID_LEAD.value, LeadState.CONSENT_REQUIRED.value,
            LeadState.DO_NOT_CONTACT.value, LeadState.MESSAGE_FAILED.value,
            LeadState.DUPLICATE_REVIEW.value}):
        next_action = _STATE_ACTIONS[status]
        if lead["status_reason"] and status != LeadState.DO_NOT_CONTACT.value:
            next_action += f": {lead['status_reason']}"
    if handover.get("active"):
        next_action = f"Salesperson to respond: {handover.get('reason')}"
    follow_up = bool(handover.get("active")) or qualification.get("classification") in {"HOT", "WARM", "NEEDS_HUMAN_REVIEW"} \
        or status in {LeadState.CONSENT_REQUIRED.value, LeadState.MESSAGE_FAILED.value,
                      LeadState.INVALID_LEAD.value, LeadState.DUPLICATE_REVIEW.value}
    if status == LeadState.DO_NOT_CONTACT.value:
        follow_up = False

    def _s(value: Any) -> str:
        return "" if value in (None, "Unknown") else str(value)

    return {
        "agent_status": status,
        "first_contact_at": _s(lead["first_contact_at"]),
        "last_message_at": _s(lead["last_message_at"]),
        "conversation_status": _s(conv["status"] if conv else ""),
        "conversation_summary": _s(summary["summary_text"] if summary else ""),
        "qualification_status": _s(qualification.get("classification")),
        "qualification_reason": "; ".join(qualification.get("reasons") or []),
        "product_interest": ", ".join(memory.get("products_of_interest") or []),
        "quoted_price": "; ".join(quoted),
        "price_source": "; ".join(sources),
        "purchase_timeline": _s(facts.get("purchase_timeline")),
        "next_action": _s(next_action),
        "follow_up_required": "Yes" if follow_up else "No",
        "assigned_to": _s(lead["assigned_to"]),
        "closed_at": _s(lead["closed_at"]),
        "last_error": _s(lead["last_error"]),
    }


class CsvSink:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def upsert(self, lead_id: str, output_values: dict[str, str]) -> str:
        found = {"value": False}

        def mutate(doc: CsvDocument) -> bool:
            header_added = False
            for col in OUTPUT_COLUMNS:
                if col not in doc.columns:
                    doc.columns.append(col)
                    doc.original_headers.append(col)
                    header_added = True
            ids = assign_row_identities(doc.rows)
            changed = False
            for row, (row_id, generated) in zip(doc.rows, ids):
                if row_id != lead_id:
                    continue
                found["value"] = True
                if generated and not row.get("lead_id"):
                    if "lead_id" not in doc.columns:
                        doc.columns.insert(0, "lead_id")
                        doc.original_headers.insert(0, "lead_id")
                    row["lead_id"] = lead_id
                    changed = True
                for col, val in output_values.items():
                    if row.get(col, "") != val:
                        row[col] = val
                        changed = True
                break
            return (changed or header_added) and found["value"]

        changed = locked_update(self.path, mutate)
        if not found["value"]:
            raise PermanentJobError(f"row for lead {lead_id} no longer exists in {self.path.name}")
        return "updated" if changed else "unchanged"


class SyncService:
    def __init__(self, db: Database, queue: JobQueue, sinks: list[str],
                 csv_sink: CsvSink | None = None,
                 sheets_sink_factory: Callable[[], GoogleSheetsSink] | None = None) -> None:
        self.db = db
        self.queue = queue
        self.sinks = sinks
        self.csv_sink = csv_sink
        self._sheets_factory = sheets_sink_factory
        self._sheets: GoogleSheetsSink | None = None

    def request_sync(self, lead_id: str, conn: sqlite3.Connection | None = None) -> None:
        def _do(c: sqlite3.Connection) -> None:
            for sink in self.sinks:
                c.execute(
                    "INSERT INTO sync_status(lead_id, sink, desired_version, synced_version, status, "
                    "updated_at) VALUES (?, ?, 1, 0, 'PENDING', ?) ON CONFLICT(lead_id, sink) DO UPDATE "
                    "SET desired_version=desired_version+1, status='PENDING', updated_at=excluded.updated_at",
                    (lead_id, sink, now_iso()))
                version = c.execute("SELECT desired_version FROM sync_status WHERE lead_id=? AND sink=?",
                                    (lead_id, sink)).fetchone()["desired_version"]
                self.queue.enqueue(JOB_SYNC_OUTPUT, {"lead_id": lead_id, "sink": sink},
                                   dedupe_key=f"sync:{lead_id}:{sink}:{version}",
                                   serial_key=f"sync:{lead_id}:{sink}", conn=c)

        if conn is not None:
            _do(conn)
        else:
            with self.db.tx() as c:
                _do(c)

    def _sheets_sink(self) -> GoogleSheetsSink:
        if self._sheets is None:
            if self._sheets_factory is None:
                raise PermanentJobError("Google Sheets sink is not configured")
            self._sheets = self._sheets_factory()
        return self._sheets

    def handle_job(self, job: Job, on_all_synced: Callable[[str], None] | None = None) -> None:
        lead_id, sink = job.payload["lead_id"], job.payload["sink"]
        state = self.db.query_one("SELECT * FROM sync_status WHERE lead_id=? AND sink=?", (lead_id, sink))
        if state is None or state["synced_version"] >= state["desired_version"]:
            return  # an earlier job already wrote the latest values
        target_version = state["desired_version"]
        values = build_output_values(self.db, lead_id)  # always the latest durable state
        try:
            if sink == "csv":
                if self.csv_sink is None:
                    raise PermanentJobError("CSV sink is not configured")
                self.csv_sink.upsert(lead_id, values)
            elif sink == "sheets":
                lead = self.db.query_one("SELECT raw_json FROM leads WHERE lead_id=?", (lead_id,))
                raw = loads(lead["raw_json"], {}) if lead else {}
                inputs = {c: str(raw.get(c, "")) for c in INPUT_COLUMNS if raw.get(c)}
                inputs["lead_id"] = lead_id
                self._sheets_sink().upsert(lead_id, inputs, values)
            else:
                raise PermanentJobError(f"unknown sink {sink}")
        except (PermanentJobError, CsvInvalid) as exc:
            self._mark(lead_id, sink, "FAILED", str(exc))
            if job.attempts >= job.max_attempts or isinstance(exc, PermanentJobError):
                raise PermanentJobError(str(exc)) from exc
            raise
        except Exception as exc:
            self._mark(lead_id, sink, "FAILED", f"{type(exc).__name__}: {exc}")
            raise
        with self.db.tx() as c:
            c.execute("UPDATE sync_status SET synced_version=MAX(synced_version, ?), "
                      "status=CASE WHEN desired_version<=? THEN 'SYNCED' ELSE 'PENDING' END, "
                      "attempts=0, last_error=NULL, updated_at=? WHERE lead_id=? AND sink=?",
                      (target_version, target_version, now_iso(), lead_id, sink))
        if on_all_synced and self.all_synced(lead_id):
            on_all_synced(lead_id)

    def _mark(self, lead_id: str, sink: str, status: str, error: str) -> None:
        self.db.execute("UPDATE sync_status SET status=?, attempts=attempts+1, last_error=?, "
                        "updated_at=? WHERE lead_id=? AND sink=?",
                        (status, error[:1000], now_iso(), lead_id, sink))

    def all_synced(self, lead_id: str) -> bool:
        rows = self.db.query("SELECT sink, status FROM sync_status WHERE lead_id=?", (lead_id,))
        by_sink = {r["sink"]: r["status"] for r in rows}
        return all(by_sink.get(s) == "SYNCED" for s in self.sinks)
