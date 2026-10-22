"""Product catalogue storage and idempotent (re-)ingestion.

Each source file is tracked by content hash. Re-running ingestion only re-parses
changed files; a changed file's products and chunks are replaced atomically, and
files that were deleted from the knowledge-base folder have their data removed,
so stale prices are never served from a document that no longer exists.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.knowledge.document_loader import SUPPORTED_SUFFIXES, DocumentLoader, LoadedDocument
from app.models.product import Product, model_key
from app.repositories.database import Database, now_iso

log = logging.getLogger(__name__)

PRODUCT_COLUMNS = [
    "product_id", "product_name", "category", "model", "configuration", "description",
    "technical_specifications", "applications", "listed_price", "currency", "tax_included",
    "tax_rate", "installation_charges", "shipping_charges", "warranty", "availability",
    "minimum_order_quantity", "price_valid_until", "price_conditions", "source_file",
    "source_reference", "last_updated_at",
]


@dataclass
class IngestReport:
    ingested: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _row_to_product(row) -> Product:
    data = {c: row[c] for c in PRODUCT_COLUMNS}
    if data["tax_included"] is not None:
        data["tax_included"] = bool(data["tax_included"])
    return Product(**data)


class ProductRepository:
    def __init__(self, db: Database, loader: DocumentLoader) -> None:
        self.db = db
        self.loader = loader

    # --- ingestion -------------------------------------------------------------
    def ingest_directory(self, directory: Path, force: bool = False) -> IngestReport:
        report = IngestReport()
        directory.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in directory.rglob("*")
                       if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
                       and not p.name.startswith((".", "~$")))
        present = {str(p.relative_to(directory)) for p in files}

        known = {r["source_file"]: r["file_hash"] for r in self.db.query(
            "SELECT source_file, file_hash FROM kb_sources")}
        for removed in sorted(set(known) - present):
            with self.db.tx() as conn:
                conn.execute("DELETE FROM products WHERE source_file=?", (removed,))
                conn.execute("DELETE FROM doc_chunks WHERE source_file=?", (removed,))
                conn.execute("DELETE FROM kb_sources WHERE source_file=?", (removed,))
            report.removed.append(removed)

        for path in files:
            name = str(path.relative_to(directory))
            digest = file_hash(path)
            if not force and known.get(name) == digest:
                self.db.execute("UPDATE kb_sources SET ingested_at=? WHERE source_file=?",
                                (now_iso(), name))
                report.unchanged.append(name)
                continue
            loaded = self.loader.load(path, display_name=name)
            self._store(loaded, digest, path)
            if loaded.error:
                report.errors[name] = loaded.error
            else:
                report.ingested.append(name)
            if loaded.warnings:
                report.warnings[name] = loaded.warnings

        if report.ingested or report.removed:
            self.db.set_meta("kb_version", now_iso())
            log.info("Knowledge base updated: %d ingested, %d removed, %d unchanged",
                     len(report.ingested), len(report.removed), len(report.unchanged))
        return report

    def _store(self, loaded: LoadedDocument, digest: str, path: Path) -> None:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        with self.db.tx() as conn:
            if loaded.error:
                # Keep the previous good data for this file, but record the failure.
                conn.execute(
                    "INSERT INTO kb_sources(source_file, file_hash, file_mtime, ingested_at, status, "
                    "message) VALUES (?, ?, ?, ?, 'ERROR', ?) ON CONFLICT(source_file) DO UPDATE "
                    "SET status='ERROR', message=excluded.message, ingested_at=excluded.ingested_at",
                    (loaded.source_file, "", mtime, now_iso(), loaded.error))
                return
            conn.execute("DELETE FROM products WHERE source_file=?", (loaded.source_file,))
            conn.execute("DELETE FROM doc_chunks WHERE source_file=?", (loaded.source_file,))
            for p in loaded.products:
                values = [getattr(p, c) for c in PRODUCT_COLUMNS]
                values[PRODUCT_COLUMNS.index("tax_included")] = (
                    None if p.tax_included is None else int(p.tax_included))
                conn.execute(
                    f"INSERT INTO products({', '.join(PRODUCT_COLUMNS)}, model_key) "
                    f"VALUES ({', '.join('?' * (len(PRODUCT_COLUMNS) + 1))})",
                    [*values, model_key(p.model)])
            for index, (ref, text) in enumerate(loaded.chunks):
                conn.execute(
                    "INSERT INTO doc_chunks(source_file, chunk_index, source_reference, text) "
                    "VALUES (?, ?, ?, ?)", (loaded.source_file, index, ref, text))
            conn.execute(
                "INSERT INTO kb_sources(source_file, file_hash, file_mtime, ingested_at, status, "
                "message, product_count, chunk_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_file) DO UPDATE SET file_hash=excluded.file_hash, "
                "file_mtime=excluded.file_mtime, ingested_at=excluded.ingested_at, "
                "status=excluded.status, message=excluded.message, "
                "product_count=excluded.product_count, chunk_count=excluded.chunk_count",
                (loaded.source_file, digest, mtime, now_iso(), loaded.status,
                 "; ".join(loaded.warnings) or None, len(loaded.products), len(loaded.chunks)))

    # --- queries -----------------------------------------------------------------
    def all_products(self) -> list[Product]:
        return [_row_to_product(r) for r in self.db.query("SELECT * FROM products ORDER BY row_id")]

    def by_model_key(self, key: str) -> list[Product]:
        return [_row_to_product(r) for r in self.db.query(
            "SELECT * FROM products WHERE model_key=? ORDER BY row_id", (key,))]

    def by_product_id(self, product_id: str) -> list[Product]:
        return [_row_to_product(r) for r in self.db.query(
            "SELECT * FROM products WHERE UPPER(product_id)=UPPER(?) ORDER BY row_id", (product_id,))]

    def source_file_mtimes(self) -> dict[str, str]:
        return {r["source_file"]: r["file_mtime"] for r in self.db.query(
            "SELECT source_file, file_mtime FROM kb_sources")}

    def sources(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM kb_sources ORDER BY source_file")]
