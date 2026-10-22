"""Lead ingestion (source-agnostic) and the CSV file monitor.

Deduplication strategy
----------------------
* Rows with a ``lead_id`` are identified by it.
* Rows without one get a deterministic id ``AUTO-<hash>`` derived from the
  normalised phone, name, company and requirement, plus the occurrence number of
  identical rows (so two identical rows are still two leads, flagged as
  duplicates rather than silently dropped). The generated id is immediately
  written back into the CSV's ``lead_id`` column so later edits keep the identity.
* Each lead stores an ``input_hash`` over the *input* columns only. Changes to
  the agent's output columns therefore never look like a new or edited lead.
* A lead id seen before is never re-triggered (across restarts, via SQLite).
  Edits to input columns are recorded as updates; they are only re-processed if
  ``CSV_REPROCESS_EDITED_ROWS=true``.
* On the very first start, rows already present are recorded as
  ``SKIPPED_EXISTING`` unless ``CSV_PROCESS_EXISTING_ON_STARTUP=true``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ingestion.csv_file import (
    CsvDocument, CsvInvalid, CsvNotReady, file_signature, locked_update, read_csv_document,
)
from app.ingestion.lead_validator import identity_hash, normalize_phone
from app.logging_utils import mask_phone
from app.models.lead import INPUT_COLUMNS, OUTPUT_COLUMNS, REQUIRED_COLUMNS, LeadState
from app.repositories.database import Database, dumps, now_iso
from app.repositories.lead_repository import LeadRepository
from app.services.message_queue import JobQueue

log = logging.getLogger(__name__)

JOB_CONTACT_LEAD = "CONTACT_LEAD"


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def assign_row_identities(rows: list[dict[str, str]]) -> list[tuple[str, bool]]:
    """Return ``(lead_id, generated)`` for every row, deterministically."""
    out: list[tuple[str, bool]] = []
    occurrences: dict[str, int] = {}
    for row in rows:
        lead_id = (row.get("lead_id") or "").strip()
        if lead_id:
            out.append((lead_id, False))
            continue
        content = "|".join([
            re.sub(r"\D", "", row.get("mobile_number", "")),
            _norm(row.get("customer_name")), _norm(row.get("company_name")),
            _norm(row.get("requirement")),
        ])
        occurrences[content] = occurrences.get(content, 0) + 1
        digest = hashlib.sha256(f"{content}#{occurrences[content]}".encode()).hexdigest()
        out.append((f"AUTO-{digest[:10].upper()}", True))
    return out


def input_hash(row: dict[str, str]) -> str:
    """Hash of input columns only (output columns and the id itself are excluded)."""
    excluded = {"lead_id", *OUTPUT_COLUMNS}
    items = sorted((k, (v or "").strip()) for k, v in row.items() if k not in excluded)
    return hashlib.sha256(dumps(items).encode()).hexdigest()


@dataclass
class IngestResult:
    new: list[str] = field(default_factory=list)
    baselined: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    reprocessed: list[str] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)


class LeadIngestor:
    """Turns source rows into lead records and CONTACT_LEAD jobs. Idempotent."""

    def __init__(self, settings: Settings, db: Database, queue: JobQueue) -> None:
        self.settings = settings
        self.db = db
        self.queue = queue
        self.leads = LeadRepository(db)

    def ingest(self, rows: list[dict[str, str]], source: str, *, baseline: bool = False) -> IngestResult:
        result = IngestResult()
        identities = assign_row_identities(rows)
        seen: set[str] = set()
        for row, (lead_id, generated) in zip(rows, identities):
            if lead_id in seen:
                result.warnings.append(f"duplicate lead_id '{lead_id}' in source; later row ignored")
                self.db.audit("duplicate_lead_id_in_source", lead_id=lead_id, source=source)
                continue
            seen.add(lead_id)
            try:
                self._ingest_row(row, lead_id, generated, source, baseline, result)
            except Exception as exc:  # noqa: BLE001 - one bad row must not block others
                log.exception("Failed to ingest lead %s", lead_id)
                result.warnings.append(f"lead {lead_id}: {exc}")
        if result.new or result.updated or result.reprocessed:
            log.info("Ingested from %s: %d new, %d baselined, %d updated, %d reprocessed",
                     source, len(result.new), len(result.baselined), len(result.updated),
                     len(result.reprocessed))
        return result

    def _fields(self, row: dict[str, str]) -> dict[str, Any]:
        mobile = row.get("mobile_number", "")
        e164 = normalize_phone(mobile, self.settings.default_country_code)
        extra = {k: v for k, v in row.items() if k not in INPUT_COLUMNS and k not in OUTPUT_COLUMNS}
        return dict(
            input_hash=input_hash(row),
            identity_hash=identity_hash(e164 or mobile, row.get("requirement")),
            customer_name=row.get("customer_name", ""),
            company_name=row.get("company_name", ""),
            mobile_raw=mobile,
            mobile_e164=e164,
            requirement=row.get("requirement", ""),
            email=row.get("email", ""),
            city=row.get("city", ""),
            state=row.get("state", ""),
            industry=row.get("industry", ""),
            preferred_language=row.get("preferred_language", ""),
            consent_status=row.get("consent_status", ""),
            lead_source=row.get("source", ""),
            raw_json=dumps({**{c: row.get(c, "") for c in INPUT_COLUMNS}, "extra": extra}),
        )

    def _ingest_row(self, row: dict[str, str], lead_id: str, generated: bool, source: str,
                    baseline: bool, result: IngestResult) -> None:
        fields = self._fields(row)
        with self.db.tx() as conn:
            existing = self.leads.get(lead_id, conn)
            if existing is None:
                status = LeadState.SKIPPED_EXISTING if baseline else LeadState.NEW_LEAD
                self.leads.insert(conn, lead_id, source=source, id_generated=int(generated),
                                  status=status.value, **fields)
                self.db.audit("lead_detected", lead_id=lead_id, conn=conn, source=source,
                              baseline=baseline, generated_id=generated,
                              mobile=mask_phone(fields["mobile_raw"]))
                if baseline:
                    result.baselined.append(lead_id)
                else:
                    self.queue.enqueue(JOB_CONTACT_LEAD, {"lead_id": lead_id},
                                       dedupe_key=f"contact:{lead_id}", conn=conn)
                    result.new.append(lead_id)
                return
            if existing["input_hash"] == fields["input_hash"]:
                result.unchanged += 1
                return
            # Edited input row: update stored data; never treated as a new lead.
            self.leads.update(lead_id, conn=conn, **fields)
            self.db.audit("lead_input_changed", lead_id=lead_id, conn=conn, source=source)
            result.updated.append(lead_id)
            if self.settings.csv_reprocess_edited_rows:
                self.queue.enqueue(
                    JOB_CONTACT_LEAD, {"lead_id": lead_id, "reprocess": True},
                    dedupe_key=f"contact:{lead_id}:{fields['input_hash'][:16]}", conn=conn)
                result.reprocessed.append(lead_id)


class CsvLeadMonitor:
    """Polls (and optionally watches) the lead CSV and feeds new rows to the ingestor."""

    def __init__(self, settings: Settings, db: Database, ingestor: LeadIngestor) -> None:
        self.settings = settings
        self.db = db
        self.ingestor = ingestor
        self.path = Path(settings.leads_csv_path)
        self._last_signature: tuple[int, int, int] | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None
        self.last_error: str | None = None

    @property
    def baseline_key(self) -> str:
        return f"csv_baseline:{self.path.resolve()}"

    def initialize(self) -> None:
        """Establish the startup baseline exactly once per file path."""
        if self.db.get_meta(self.baseline_key) is not None:
            return
        if self.settings.csv_process_existing_on_startup or not self.path.exists():
            self.db.set_meta(self.baseline_key, now_iso())
            return
        try:
            result = self.scan(force=True, baseline=True)
        except (CsvNotReady, CsvInvalid) as exc:
            log.warning("Baseline scan postponed: %s", exc)
            return
        if result is None:
            return  # file still settling; the monitor loop retries the baseline
        # scan() records the baseline marker after a successful baseline ingest.

    def scan(self, force: bool = False, baseline: bool = False) -> IngestResult | None:
        signature = file_signature(self.path)
        if signature is None:
            return None
        if signature == self._last_signature and not force:
            return None
        if not baseline and self.db.get_meta(self.baseline_key) is None:
            baseline = not self.settings.csv_process_existing_on_startup
        settle = self.settings.csv_settle_seconds
        if settle > 0 and not baseline and (time.time() - signature[0] / 1e9) < settle:
            return None  # still being written; check again next cycle
        # (The startup baseline captures the file as it is right now.)

        doc = read_csv_document(self.path, settle_seconds=settle)
        missing = [c for c in REQUIRED_COLUMNS if c not in doc.columns]
        if missing:
            raise CsvInvalid(f"missing required column(s): {', '.join(missing)}")
        for warning in doc.warnings:
            log.warning("CSV %s: %s", self.path.name, warning)

        identities = assign_row_identities(doc.rows)
        if any(generated for _, generated in identities):
            self._write_generated_ids()
        rows = [dict(r, lead_id=lid) for r, (lid, _) in zip(doc.rows, identities)]
        result = self.ingestor.ingest(rows, source=f"csv:{self.path.name}", baseline=baseline)
        result.warnings[:0] = doc.warnings
        if baseline:
            self.db.set_meta(self.baseline_key, now_iso())
        self._last_signature = file_signature(self.path)
        self.last_error = None
        return result

    def _write_generated_ids(self) -> None:
        def mutate(doc: CsvDocument) -> bool:
            if "lead_id" not in doc.columns:
                doc.columns.insert(0, "lead_id")
                doc.original_headers.insert(0, "lead_id")
            changed = False
            for row, (lid, generated) in zip(doc.rows, assign_row_identities(doc.rows)):
                if generated and not row.get("lead_id"):
                    row["lead_id"] = lid
                    changed = True
            return changed

        try:
            locked_update(self.path, mutate)
        except (CsvNotReady, CsvInvalid, OSError) as exc:
            # Identity stays deterministic even if the write-back fails; retry next scan.
            log.warning("Could not write generated lead ids yet: %s", exc)

    # --- background operation -------------------------------------------------
    def _start_watchdog(self) -> None:
        if not self.settings.csv_use_watchdog:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.info("watchdog not installed; using polling only")
            return
        target = self.path.resolve()
        wake = self._wake

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                paths = {getattr(event, "src_path", ""), getattr(event, "dest_path", "")}
                if any(p and Path(p).resolve() == target for p in paths):
                    wake.set()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        # Watch the directory, not the file: editors often save via temp file + rename.
        observer.schedule(_Handler(), str(self.path.parent), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.db.get_meta(self.baseline_key) is None:
                    self.initialize()
                else:
                    self.scan()
            except CsvNotReady as exc:
                log.debug("CSV not ready: %s", exc)
            except CsvInvalid as exc:
                if str(exc) != self.last_error:
                    log.error("Lead CSV invalid: %s", exc)
                self.last_error = str(exc)
                self._last_signature = file_signature(self.path)  # wait for the next change
            except Exception as exc:  # noqa: BLE001
                log.exception("CSV monitor error")
                self.last_error = str(exc)
            self._wake.wait(self.settings.csv_poll_interval_seconds)
            if self._wake.is_set():
                self._wake.clear()
                self._stop.wait(self.settings.csv_settle_seconds)

    def start(self) -> None:
        self.initialize()
        self._start_watchdog()
        self._thread = threading.Thread(target=self._loop, name="csv-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout)
        if self._thread:
            self._thread.join(timeout)
