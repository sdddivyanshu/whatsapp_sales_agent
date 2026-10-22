"""SQLite persistence layer.

SQLite is the durable source of truth for leads, conversations, messages, jobs,
quotes, summaries and sync state. Each operation opens a short-lived connection
(WAL mode) so the API, CSV monitor and worker threads can share the database
safely. Writes that must be atomic use ``Database.tx()`` (BEGIN IMMEDIATE).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'csv',
    id_generated INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    customer_name TEXT,
    company_name TEXT,
    mobile_raw TEXT,
    mobile_e164 TEXT,
    requirement TEXT,
    email TEXT,
    city TEXT,
    state TEXT,
    industry TEXT,
    preferred_language TEXT,
    consent_status TEXT,
    lead_source TEXT,
    raw_json TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    delivery_status TEXT,
    duplicate_of TEXT,
    first_contact_at TEXT,
    last_message_at TEXT,
    closed_at TEXT,
    assigned_to TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_mobile ON leads(mobile_e164);
CREATE INDEX IF NOT EXISTS idx_leads_identity ON leads(identity_hash);

CREATE TABLE IF NOT EXISTS do_not_contact (
    mobile_e164 TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id),
    wa_phone TEXT NOT NULL,
    status TEXT NOT NULL,              -- ACTIVE | HUMAN | CLOSED
    automation_paused INTEGER NOT NULL DEFAULT 0,
    handover_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT,
    closure_seq INTEGER NOT NULL DEFAULT 0,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    last_processed_message_id INTEGER NOT NULL DEFAULT 0,
    memory_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(wa_phone);
CREATE INDEX IF NOT EXISTS idx_conv_lead ON conversations(lead_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    lead_id TEXT,
    direction TEXT NOT NULL,           -- inbound | outbound
    sender TEXT NOT NULL,              -- customer | agent | human | system
    wa_message_id TEXT UNIQUE,
    msg_type TEXT NOT NULL,
    body TEXT,
    status TEXT,
    error TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS status_events (
    event_key TEXT PRIMARY KEY,
    wa_message_id TEXT,
    status TEXT,
    payload_json TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    serial_key TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- PENDING | RUNNING | DONE | DEAD
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    run_after TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, run_after);

CREATE TABLE IF NOT EXISTS quotes_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT,
    conversation_id INTEGER,
    message_id INTEGER,
    product_id TEXT,
    model TEXT,
    listed_price REAL,
    currency TEXT,
    tax_included INTEGER,
    tax_rate REAL,
    source_file TEXT,
    source_reference TEXT,
    price_valid_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    lead_id TEXT,
    conversation_id INTEGER,
    event TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    lead_id TEXT NOT NULL,
    closure_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, closure_seq)
);

CREATE TABLE IF NOT EXISTS sync_status (
    lead_id TEXT NOT NULL,
    sink TEXT NOT NULL,
    desired_version INTEGER NOT NULL DEFAULT 0,
    synced_version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,              -- PENDING | SYNCED | FAILED
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (lead_id, sink)
);

CREATE TABLE IF NOT EXISTS kb_sources (
    source_file TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    file_mtime TEXT,
    ingested_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- OK | WARNING | ERROR
    message TEXT,
    product_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT,
    product_name TEXT,
    category TEXT,
    model TEXT,
    model_key TEXT,
    configuration TEXT,
    description TEXT,
    technical_specifications TEXT,
    applications TEXT,
    listed_price REAL,
    currency TEXT,
    tax_included INTEGER,
    tax_rate REAL,
    installation_charges TEXT,
    shipping_charges TEXT,
    warranty TEXT,
    availability TEXT,
    minimum_order_quantity TEXT,
    price_valid_until TEXT,
    price_conditions TEXT,
    source_file TEXT NOT NULL,
    source_reference TEXT,
    last_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_model ON products(model_key);
CREATE INDEX IF NOT EXISTS idx_products_source ON products(source_file);

CREATE TABLE IF NOT EXISTS doc_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    source_reference TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON doc_chunks(source_file);

CREATE TABLE IF NOT EXISTS outbox_mock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id TEXT,
    to_phone TEXT,
    kind TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init(self) -> None:
        with self._init_lock:
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            finally:
                conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """An IMMEDIATE (write-locking) transaction."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # --- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- audit --------------------------------------------------------------
    def audit(self, event: str, lead_id: str | None = None,
              conversation_id: int | None = None, conn: sqlite3.Connection | None = None,
              **details: Any) -> None:
        sql = ("INSERT INTO audit_log(ts, lead_id, conversation_id, event, details_json) "
               "VALUES (?, ?, ?, ?, ?)")
        params = (now_iso(), lead_id, conversation_id, event, dumps(details))
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)

    # --- maintenance ----------------------------------------------------------
    def backup(self, destination: Path | str) -> Path:
        """Online, consistent backup using SQLite's backup API."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = self.connect()
        target = sqlite3.connect(dest)
        try:
            src.backup(target)
        finally:
            target.close()
            src.close()
        return dest

    def copy_file(self, destination: Path) -> None:  # pragma: no cover - fallback
        shutil.copy2(self.path, destination)
