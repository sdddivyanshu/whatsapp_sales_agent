"""Data access for leads, conversations, messages and the do-not-contact list."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.models.lead import LeadState
from app.repositories.database import Database, dumps, loads, now_iso
from app.services.workflow import assert_transition

LEAD_FIELDS = {
    "source", "id_generated", "input_hash", "identity_hash", "customer_name",
    "company_name", "mobile_raw", "mobile_e164", "requirement", "email", "city", "state",
    "industry", "preferred_language", "consent_status", "lead_source", "raw_json", "status",
    "status_reason", "delivery_status", "duplicate_of", "first_contact_at", "last_message_at",
    "closed_at", "assigned_to", "last_error",
}


class LeadRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- leads ---------------------------------------------------------------
    def get(self, lead_id: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM leads WHERE lead_id=?"
        if conn is not None:
            return conn.execute(sql, (lead_id,)).fetchone()
        return self.db.query_one(sql, (lead_id,))

    def insert(self, conn: sqlite3.Connection, lead_id: str, **fields: Any) -> None:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"Unknown lead fields: {unknown}")
        now = now_iso()
        fields.setdefault("status", LeadState.NEW_LEAD.value)
        cols = ["lead_id", *fields.keys(), "created_at", "updated_at"]
        values = [lead_id, *fields.values(), now, now]
        conn.execute(
            f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            values,
        )

    def update(self, lead_id: str, conn: sqlite3.Connection | None = None, **fields: Any) -> None:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"Unknown lead fields: {unknown}")
        if not fields:
            return
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE leads SET {assignments} WHERE lead_id=?"
        params = [*fields.values(), lead_id]
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.db.execute(sql, tuple(params))

    def set_status(self, lead_id: str, target: LeadState, reason: str | None = None,
                   conn: sqlite3.Connection | None = None, force: bool = False) -> bool:
        """Validated state transition. Returns False if the lead is already there."""
        def _do(c: sqlite3.Connection) -> bool:
            row = c.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
            if row is None:
                raise KeyError(lead_id)
            current = LeadState(row["status"])
            if current == target:
                return False
            if not force:
                assert_transition(current, target)
            c.execute(
                "UPDATE leads SET status=?, status_reason=?, updated_at=? WHERE lead_id=?",
                (target.value, reason, now_iso(), lead_id),
            )
            self.db.audit("state_transition", lead_id=lead_id, conn=c,
                          from_state=current.value, to_state=target.value,
                          reason=reason, forced=force)
            return True

        if conn is not None:
            return _do(conn)
        with self.db.tx() as c:
            return _do(c)

    def leads_for_phone(self, e164: str) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM leads WHERE mobile_e164=? ORDER BY created_at DESC, rowid DESC",
            (e164,),
        )

    def find_by_identity(self, identity_hash: str, conn: sqlite3.Connection,
                         exclude: str | None = None) -> sqlite3.Row | None:
        """Earliest *older* lead with the same identity (the first one is never the duplicate)."""
        return conn.execute(
            "SELECT * FROM leads WHERE identity_hash=? AND lead_id != ? "
            "AND rowid < COALESCE((SELECT rowid FROM leads WHERE lead_id=?), 9223372036854775807) "
            "ORDER BY rowid ASC LIMIT 1",
            (identity_hash, exclude or "", exclude or ""),
        ).fetchone()

    def list(self, status: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if status:
            return self.db.query(
                "SELECT * FROM leads WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit))
        return self.db.query("SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,))

    # --- do not contact -------------------------------------------------------
    def add_dnc(self, e164: str, reason: str, conn: sqlite3.Connection | None = None) -> None:
        sql = ("INSERT INTO do_not_contact(mobile_e164, reason, created_at) VALUES (?, ?, ?) "
               "ON CONFLICT(mobile_e164) DO NOTHING")
        params = (e164, reason, now_iso())
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.db.execute(sql, params)

    def is_dnc(self, e164: str, conn: sqlite3.Connection | None = None) -> bool:
        sql = "SELECT 1 FROM do_not_contact WHERE mobile_e164=?"
        row = (conn.execute(sql, (e164,)).fetchone() if conn is not None
               else self.db.query_one(sql, (e164,)))
        return row is not None

    def remove_dnc(self, e164: str) -> None:
        self.db.execute("DELETE FROM do_not_contact WHERE mobile_e164=?", (e164,))


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, conversation_id: int, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM conversations WHERE conversation_id=?"
        if conn is not None:
            return conn.execute(sql, (conversation_id,)).fetchone()
        return self.db.query_one(sql, (conversation_id,))

    def latest_for_lead(self, lead_id: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        sql = ("SELECT * FROM conversations WHERE lead_id=? "
               "ORDER BY conversation_id DESC LIMIT 1")
        if conn is not None:
            return conn.execute(sql, (lead_id,)).fetchone()
        return self.db.query_one(sql, (lead_id,))

    def create(self, conn: sqlite3.Connection, lead_id: str, wa_phone: str) -> int:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO conversations(lead_id, wa_phone, status, opened_at, updated_at, memory_json) "
            "VALUES (?, ?, 'ACTIVE', ?, ?, '{}')",
            (lead_id, wa_phone, now, now),
        )
        return int(cur.lastrowid)

    def update(self, conversation_id: int, conn: sqlite3.Connection | None = None,
               **fields: Any) -> None:
        if "memory" in fields:
            fields["memory_json"] = dumps(fields.pop("memory"))
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE conversations SET {assignments} WHERE conversation_id=?"
        params = [*fields.values(), conversation_id]
        if conn is not None:
            conn.execute(sql, params)
        else:
            self.db.execute(sql, tuple(params))

    @staticmethod
    def memory(row: sqlite3.Row) -> dict[str, Any]:
        return loads(row["memory_json"], {}) or {}

    # --- messages ---------------------------------------------------------------
    def insert_message(self, conn: sqlite3.Connection, *, conversation_id: int | None,
                       lead_id: str | None, direction: str, sender: str, msg_type: str,
                       body: str | None, wa_message_id: str | None = None,
                       status: str | None = None, meta: dict | None = None) -> int | None:
        """Insert a message. Returns None if ``wa_message_id`` was already stored."""
        now = now_iso()
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages(conversation_id, lead_id, direction, sender, "
            "wa_message_id, msg_type, body, status, meta_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, lead_id, direction, sender, wa_message_id, msg_type, body,
             status, dumps(meta or {}), now, now),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def messages(self, conversation_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        if limit:
            rows = self.db.query(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit))
            return list(reversed(rows))
        return self.db.query(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))

    def unprocessed_inbound(self, conversation_id: int, after_id: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM messages WHERE conversation_id=? AND direction='inbound' AND id>? "
            "ORDER BY id", (conversation_id, after_id))

    def message_by_wamid(self, wa_message_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM messages WHERE wa_message_id=?", (wa_message_id,))
