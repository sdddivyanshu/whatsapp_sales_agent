"""Durable, SQLite-backed job queue with retries, dead-lettering and restart recovery.

* ``dedupe_key`` makes enqueueing idempotent (e.g. one CONTACT_LEAD job per lead).
* ``serial_key`` serialises jobs for the same conversation so two replies can
  never be generated concurrently for one customer.
* Failed jobs are retried with exponential backoff; after ``max_attempts`` they
  move to ``DEAD`` (the dead-letter state) and can be retried by an operator.
* Jobs left ``RUNNING`` by a crashed process are returned to ``PENDING`` on start.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from app.repositories.database import Database, dumps, loads, now_iso, parse_iso, utcnow

log = logging.getLogger(__name__)


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (job goes straight to DEAD)."""


class RetryLater(Exception):
    """Raise from a handler to reschedule without counting as a failure."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason)
        self.seconds = seconds


@dataclass
class Job:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    serial_key: str | None


class JobQueue:
    def __init__(self, db: Database, max_attempts: int = 5, backoff_base: float = 5.0,
                 stale_lock_seconds: int = 300) -> None:
        self.db = db
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.stale_lock_seconds = stale_lock_seconds

    def enqueue(self, job_type: str, payload: dict[str, Any], *, dedupe_key: str | None = None,
                serial_key: str | None = None, delay_seconds: float = 0,
                conn: sqlite3.Connection | None = None, max_attempts: int | None = None) -> int | None:
        """Enqueue a job. Returns its id, or None if ``dedupe_key`` already exists."""
        now = now_iso()
        run_after = (utcnow() + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
        sql = ("INSERT OR IGNORE INTO jobs(job_type, dedupe_key, serial_key, payload_json, status, "
               "attempts, max_attempts, run_after, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)")
        params = (job_type, dedupe_key, serial_key, dumps(payload),
                  max_attempts or self.max_attempts, run_after, now, now)
        if conn is not None:
            cur = conn.execute(sql, params)
        else:
            with self.db.tx() as c:
                cur = c.execute(sql, params)
        return int(cur.lastrowid) if cur.rowcount else None

    def claim_next(self, job_types: set[str] | None = None) -> Job | None:
        now = now_iso()
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='PENDING' AND run_after<=? ORDER BY id LIMIT 50",
                (now,),
            ).fetchall()
            running_serials = {
                r["serial_key"] for r in conn.execute(
                    "SELECT serial_key FROM jobs WHERE status='RUNNING' AND serial_key IS NOT NULL")
            }
            seen_serials: set[str] = set()
            for row in rows:
                if job_types and row["job_type"] not in job_types:
                    continue
                key = row["serial_key"]
                if key and (key in running_serials or key in seen_serials):
                    seen_serials.add(key)
                    continue  # keep FIFO order within a serial key
                conn.execute(
                    "UPDATE jobs SET status='RUNNING', attempts=attempts+1, locked_at=?, "
                    "updated_at=? WHERE id=?", (now, now, row["id"]))
                return Job(row["id"], row["job_type"], loads(row["payload_json"], {}),
                           row["attempts"] + 1, row["max_attempts"], key)
        return None

    def complete(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET status='DONE', locked_at=NULL, last_error=NULL, "
                        "updated_at=? WHERE id=?", (now_iso(), job_id))

    def reschedule(self, job: Job, seconds: float, reason: str = "") -> None:
        run_after = (utcnow() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE jobs SET status='PENDING', attempts=MAX(attempts-1,0), locked_at=NULL, "
            "run_after=?, last_error=?, updated_at=? WHERE id=?",
            (run_after, reason or None, now_iso(), job.id))

    def fail(self, job: Job, error: str, permanent: bool = False) -> str:
        if permanent or job.attempts >= job.max_attempts:
            self.db.execute("UPDATE jobs SET status='DEAD', locked_at=NULL, last_error=?, "
                            "updated_at=? WHERE id=?", (error[:2000], now_iso(), job.id))
            log.error("Job %s (%s) moved to DEAD: %s", job.id, job.job_type, error)
            return "DEAD"
        delay = self.backoff_base * (2 ** (job.attempts - 1))
        run_after = (utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds")
        self.db.execute("UPDATE jobs SET status='PENDING', locked_at=NULL, last_error=?, "
                        "run_after=?, updated_at=? WHERE id=?",
                        (error[:2000], run_after, now_iso(), job.id))
        log.warning("Job %s (%s) failed (attempt %s/%s), retry in %.0fs: %s",
                    job.id, job.job_type, job.attempts, job.max_attempts, delay, error)
        return "PENDING"

    def recover_stale(self, force_all: bool = False) -> int:
        """Return RUNNING jobs to PENDING (all of them on startup, else only stale ones)."""
        cutoff = utcnow() - timedelta(seconds=self.stale_lock_seconds)
        recovered = 0
        with self.db.tx() as conn:
            for row in conn.execute("SELECT id, locked_at FROM jobs WHERE status='RUNNING'").fetchall():
                locked = parse_iso(row["locked_at"])
                if force_all or locked is None or locked < cutoff:
                    conn.execute("UPDATE jobs SET status='PENDING', locked_at=NULL, updated_at=? "
                                 "WHERE id=?", (now_iso(), row["id"]))
                    recovered += 1
        if recovered:
            log.info("Recovered %d interrupted job(s)", recovered)
        return recovered

    def retry_dead(self, job_id: int | None = None) -> int:
        sql = ("UPDATE jobs SET status='PENDING', attempts=0, run_after=?, updated_at=? "
               "WHERE status='DEAD'")
        params: tuple = (now_iso(), now_iso())
        if job_id is not None:
            sql += " AND id=?"
            params += (job_id,)
        with self.db.tx() as conn:
            return conn.execute(sql, params).rowcount

    def stats(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


Handler = Callable[[Job], None]


class Worker:
    """Runs queued jobs in a background thread until ``stop()`` is called."""

    def __init__(self, queue: JobQueue, handlers: dict[str, Handler],
                 poll_interval: float = 1.0,
                 periodic: list[tuple[float, Callable[[], None]]] | None = None) -> None:
        self.queue = queue
        self.handlers = handlers
        self.poll_interval = poll_interval
        self.periodic = [(interval, fn, [0.0]) for interval, fn in (periodic or [])]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was processed."""
        job = self.queue.claim_next(set(self.handlers))
        if job is None:
            return False
        handler = self.handlers[job.job_type]
        try:
            handler(job)
        except RetryLater as exc:
            self.queue.reschedule(job, exc.seconds, str(exc))
        except PermanentJobError as exc:
            self.queue.fail(job, f"permanent: {exc}", permanent=True)
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            log.exception("Job %s (%s) raised", job.id, job.job_type)
            self.queue.fail(job, f"{type(exc).__name__}: {exc}")
        else:
            self.queue.complete(job.id)
        return True

    def drain(self, max_jobs: int = 1000) -> int:
        """Process jobs until none are runnable (used by tests and scripts)."""
        count = 0
        while count < max_jobs and self.run_once():
            count += 1
        return count

    def run_periodic(self) -> None:
        import time
        now = time.monotonic()
        for interval, fn, last in self.periodic:
            if now - last[0] >= interval:
                last[0] = now
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("Periodic task %s failed", getattr(fn, "__name__", fn))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_periodic()
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 - e.g. database locked
                log.exception("Worker loop error")
                busy = False
            if not busy:
                self._stop.wait(self.poll_interval)

    def start(self) -> None:
        self.queue.recover_stale(force_all=True)
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
