"""Robust CSV reading and corruption-safe CSV writing.

Reading
    * encoding fallback (utf-8-sig, utf-8, cp1252, latin-1) with BOM detection
    * header normalisation (``Mobile Number`` -> ``mobile_number``)
    * duplicate header columns (first one wins, others reported)
    * repeated header lines inside the data (e.g. two files concatenated)
    * blank rows, short rows (padded) and malformed long rows (reported, skipped)

Writing
    * an advisory lock file (``<name>.lock``) serialises our own writers
    * the file is **re-read under the lock** right before every write so
      concurrent edits to other rows/columns are preserved
    * a temp file in the same directory is fsync'd and atomically ``os.replace``d
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import pandas as pd

log = logging.getLogger(__name__)

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

HEADER_ALIASES = {
    "mobile": "mobile_number", "phone": "mobile_number", "phone_number": "mobile_number",
    "whatsapp": "mobile_number", "whatsapp_number": "mobile_number", "contact_number": "mobile_number",
    "name": "customer_name", "customer": "customer_name", "contact_name": "customer_name",
    "company": "company_name", "organisation": "company_name", "organization": "company_name",
    "enquiry": "requirement", "inquiry": "requirement", "requirements": "requirement",
    "consent": "consent_status", "opt_in": "consent_status", "language": "preferred_language",
    "id": "lead_id", "leadid": "lead_id",
}


class CsvNotReady(Exception):
    """The file is missing, locked or still being written; try again later."""


class CsvInvalid(Exception):
    """The file cannot be interpreted as a lead sheet (e.g. missing required columns)."""


@dataclass
class CsvDocument:
    columns: list[str]                     # normalised column names, in file order
    rows: list[dict[str, str]]             # all values as stripped strings
    original_headers: list[str]
    encoding: str
    had_bom: bool
    warnings: list[str] = field(default_factory=list)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns, dtype=str).fillna("")


def normalize_header(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return HEADER_ALIASES.get(key, key)


def _decode(raw: bytes) -> tuple[str, str, bool]:
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc, had_bom
        except UnicodeDecodeError:
            continue
    raise CsvInvalid("unable to decode file")  # latin-1 never fails; defensive


def read_csv_document(path: Path, settle_seconds: float = 0.0) -> CsvDocument:
    try:
        stat = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise CsvNotReady(f"{path} does not exist") from exc
    except (PermissionError, OSError) as exc:  # locked by Excel etc.
        raise CsvNotReady(f"{path} is not readable yet: {exc}") from exc

    text, encoding, had_bom = _decode(raw)
    if text.startswith("﻿"):
        text = text[1:]
    if text and not text.endswith(("\n", "\r")) and settle_seconds > 0:
        if time.time() - stat.st_mtime < settle_seconds * 2:
            raise CsvNotReady("last line is incomplete; waiting for writer to finish")
    if not text.strip():
        raise CsvInvalid("file is empty")

    warnings: list[str] = []
    bad_lines: list[list[str]] = []

    def _on_bad_line(line: list[str]) -> None:
        bad_lines.append(line)
        return None

    try:
        frame = pd.read_csv(
            io.StringIO(text), header=None, dtype=str, keep_default_na=False,
            skip_blank_lines=True, engine="python", on_bad_lines=_on_bad_line,
        )
    except pd.errors.EmptyDataError as exc:
        raise CsvInvalid("file has no rows") from exc
    except (pd.errors.ParserError, csv.Error, ValueError) as exc:
        raise CsvInvalid(f"malformed CSV: {exc}") from exc

    if bad_lines:
        warnings.append(f"skipped {len(bad_lines)} malformed line(s) with too many fields")
    frame = frame.fillna("")
    original_headers = [str(h).strip() for h in frame.iloc[0].tolist()]
    normalized = [normalize_header(h) for h in original_headers]

    columns: list[str] = []
    keep_positions: list[int] = []
    for pos, name in enumerate(normalized):
        if not name:
            name = f"unnamed_{pos}"
        if name in columns:
            warnings.append(f"duplicate column '{original_headers[pos]}' ignored (first one kept)")
            continue
        columns.append(name)
        keep_positions.append(pos)

    rows: list[dict[str, str]] = []
    header_signature = [h.lower() for h in original_headers]
    for _, series in frame.iloc[1:].iterrows():
        values = [str(v).strip() for v in series.tolist()]
        if not any(values):
            continue  # blank row
        if [v.lower() for v in values] == header_signature:
            warnings.append("repeated header line skipped")
            continue
        rows.append({columns[i]: values[pos] for i, pos in enumerate(keep_positions)})

    return CsvDocument(columns, rows, original_headers, encoding, had_bom, warnings)


def file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


@contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Advisory lock using a sidecar ``.lock`` file (fcntl when available)."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        try:
            import fcntl

            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise CsvNotReady(f"timed out waiting for {lock_path}") from None
                    time.sleep(0.05)
        except ImportError:  # pragma: no cover - Windows: rely on atomic replace only
            pass
        yield
    finally:
        fh.close()


def atomic_write_document(path: Path, doc: CsvDocument) -> None:
    """Write ``doc`` back using its original headers for original columns."""
    header_map = dict(zip(
        [normalize_header(h) or f"unnamed_{i}" for i, h in enumerate(doc.original_headers)],
        doc.original_headers,
    ))
    out_headers = [header_map.get(c, c) for c in doc.columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(out_headers)
    for row in doc.rows:
        writer.writerow([row.get(c, "") for c in doc.columns])
    encoding = "utf-8-sig" if doc.had_bom else ("utf-8" if doc.encoding.startswith("utf") else doc.encoding)
    data = buffer.getvalue().encode(encoding, errors="replace")

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def locked_update(path: Path, mutate: Callable[[CsvDocument], bool]) -> bool:
    """Reload the CSV under lock, apply ``mutate`` and atomically write if it changed."""
    with file_lock(path):
        doc = read_csv_document(path)
        if doc.warnings:
            # Malformed lines would be lost by a rewrite: refuse instead of corrupting.
            # Same for duplicate columns (only the first is kept in memory).
            lossy = [w for w in doc.warnings if "malformed" in w or "duplicate column" in w]
            if lossy:
                raise CsvInvalid("refusing to rewrite CSV, it would lose data: "
                                 + "; ".join(lossy) + ". Fix the file and the write will retry.")
        changed = mutate(doc)
        if changed:
            atomic_write_document(path, doc)
        return changed
