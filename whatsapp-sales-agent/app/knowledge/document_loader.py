"""Catalogue / price-list document parsing.

Supported: CSV, XLSX/XLSM, PDF, DOCX, TXT/MD, plus PNG/JPG when OCR is enabled.

Tables that look like price lists (they have a model / product id column plus a
price or name column) become structured :class:`Product` records. Everything
else becomes text chunks for retrieval. Values that are missing or unparseable
stay ``None`` (unknown) - nothing is defaulted or guessed. Scanned PDFs are
detected and reported; they are OCR'd only when ``OCR_ENABLED=true``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.models.product import Product

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".pdf", ".docx", ".txt", ".md",
                      ".png", ".jpg", ".jpeg"}

PRODUCT_HEADER_ALIASES: dict[str, list[str]] = {
    "product_id": ["product_id", "sku", "item_code", "product_code", "code", "part_no", "id"],
    "product_name": ["product_name", "product", "machine", "machine_name", "name", "item",
                     "item_name", "description_short"],
    "category": ["category", "product_category", "type", "machine_type", "segment"],
    "model": ["model", "model_no", "model_number", "model_name", "machine_model"],
    "configuration": ["configuration", "variant", "config", "version"],
    "description": ["description", "details", "overview"],
    "technical_specifications": ["technical_specifications", "specifications", "specs",
                                 "spec", "technical_specs", "key_specifications"],
    "applications": ["applications", "application", "uses", "suitable_for"],
    "listed_price": ["listed_price", "price", "list_price", "base_price", "rate", "price_inr",
                     "ex_works_price", "unit_price", "mrp", "price_rs", "basic_price",
                     "price_excl_gst", "price_ex_gst", "price_incl_gst", "price_inclusive_gst"],
    "currency": ["currency", "curr"],
    "tax_included": ["tax_included", "gst_included", "gst", "tax", "gst_inclusive",
                     "tax_treatment", "gst_treatment"],
    "tax_rate": ["tax_rate", "gst_rate", "gst_percent", "gst_pct", "gst_percentage"],
    "installation_charges": ["installation_charges", "installation", "installation_cost",
                             "commissioning"],
    "shipping_charges": ["shipping_charges", "shipping", "freight", "transport",
                         "transportation", "delivery_charges"],
    "warranty": ["warranty", "warranty_period"],
    "availability": ["availability", "stock", "stock_status", "lead_time"],
    "minimum_order_quantity": ["minimum_order_quantity", "moq", "min_order_qty"],
    "price_valid_until": ["price_valid_until", "valid_until", "validity", "valid_till",
                          "price_validity"],
    "price_conditions": ["price_conditions", "terms", "conditions", "remarks", "notes"],
}

_ALIAS_LOOKUP = {alias: canonical for canonical, aliases in PRODUCT_HEADER_ALIASES.items()
                 for alias in aliases}


@dataclass
class LoadedDocument:
    source_file: str
    products: list[Product] = field(default_factory=list)
    chunks: list[tuple[str, str]] = field(default_factory=list)  # (reference, text)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        return "WARNING" if self.warnings else "OK"


# ---------------------------------------------------------------------------
# value parsing
# ---------------------------------------------------------------------------

def _header_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "n/a", "na", "-", "--", "tbd", "unknown"}:
        return None
    return text


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    if re.search(r"₹|\bRs\.?|\bINR\b|\brupees?\b", text, re.I):
        return "INR"
    if re.search(r"US\$|\bUSD\b|\$", text):
        return "USD"
    if re.search(r"€|\bEUR\b", text):
        return "EUR"
    return None


def parse_price(value: Any) -> float | None:
    """Parse a price cell. Returns None for 'on request', ranges and anything unclear."""
    text = _clean(value)
    if text is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    lower = text.lower()
    if re.search(r"request|enquire|inquire|contact|call|poa|negotiable|tba|on\s*demand", lower):
        return None
    if re.search(r"\d\s*(-|–|to)\s*\d", lower.replace(",", "")):
        return None  # ranges are not a quotable listed price
    multiplier = 1.0
    if re.search(r"\b(lakh|lac|lakhs|lacs|l)\b", lower):
        multiplier = 100_000.0
    elif re.search(r"\b(crore|cr)\b", lower):
        multiplier = 10_000_000.0
    # Percentages (e.g. "+ GST 18%") are tax information, not part of the price.
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", re.sub(r"\d+(?:\.\d+)?\s*%", "", text))
    if len(numbers) != 1:
        return None
    try:
        amount = float(numbers[0].replace(",", "")) * multiplier
    except ValueError:
        return None
    return amount if amount > 0 else None


def parse_tax_included(value: Any, header: str = "") -> bool | None:
    text = (_clean(value) or "").lower()
    if text:
        if re.search(r"\b(excl|exclusive|extra|plus|additional|not included)\b|\+\s*gst|\bno\b", text):
            return False
        if re.search(r"\b(incl|inclusive|included|including)\b|\byes\b", text):
            return True
    header = header.lower()
    if re.search(r"incl", header) and "price" in header:
        return True
    if re.search(r"excl|ex_gst", header) and "price" in header:
        return False
    return None


def parse_rate(value: Any) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    rate = float(match.group(1))
    return rate if 0 <= rate <= 100 else None


def parse_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _clean(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                "%b %d %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# table -> products
# ---------------------------------------------------------------------------

def map_product_headers(headers: Iterable[Any]) -> dict[int, tuple[str, str]]:
    """Return {column position: (canonical field, header key)} for recognised headers."""
    mapping: dict[int, tuple[str, str]] = {}
    used: set[str] = set()
    for pos, header in enumerate(headers):
        key = _header_key(header)
        canonical = _ALIAS_LOOKUP.get(key)
        if canonical is None:
            # loose match, e.g. "Price (INR, ex-works)" -> price_inr_ex_works
            for alias, cand in _ALIAS_LOOKUP.items():
                if cand in {"listed_price", "price_valid_until", "tax_rate"} and key.startswith(alias):
                    canonical = cand
                    break
        if canonical and canonical not in used:
            mapping[pos] = (canonical, key)
            used.add(canonical)
    return mapping


def is_product_table(mapping: dict[int, tuple[str, str]]) -> bool:
    fields = {f for f, _ in mapping.values()}
    return bool({"model", "product_id"} & fields) and bool({"listed_price", "product_name"} & fields)


def rows_to_products(headers: list[Any], rows: list[list[Any]], source_file: str,
                     reference_prefix: str, default_currency: str | None,
                     last_updated_at: str | None, warnings: list[str]) -> list[Product]:
    mapping = map_product_headers(headers)
    products: list[Product] = []
    for idx, row in enumerate(rows):
        values: dict[str, Any] = {}
        headers_for: dict[str, str] = {}
        for pos, (canonical, header_key) in mapping.items():
            if pos < len(row):
                values[canonical] = row[pos]
                headers_for[canonical] = header_key
        if not any(_clean(v) for v in values.values()):
            continue
        raw_price = values.get("listed_price")
        price = parse_price(raw_price)
        raw_price_text = _clean(raw_price)
        currency = (_clean(values.get("currency")) or "").upper() or detect_currency(raw_price_text) \
            or detect_currency(headers_for.get("listed_price", "").replace("_", " ")) \
            or (default_currency if price is not None else None)
        if currency and len(currency) > 3:
            currency = detect_currency(currency)
        tax_included = parse_tax_included(values.get("tax_included"), "")
        if tax_included is None:
            tax_included = parse_tax_included(raw_price_text, headers_for.get("listed_price", ""))
        tax_rate = parse_rate(values.get("tax_rate"))
        tax_cell = _clean(values.get("tax_included"))
        if tax_rate is None and tax_cell and re.fullmatch(r"\d+(\.\d+)?\s*%", tax_cell):
            tax_rate = parse_rate(tax_cell)
        if tax_rate is None and raw_price_text:
            m = re.search(r"gst\D{0,6}(\d+(?:\.\d+)?)\s*%", raw_price_text, re.I)
            tax_rate = float(m.group(1)) if m else None
        conditions = _clean(values.get("price_conditions"))
        if raw_price_text and price is None:
            conditions = "; ".join(filter(None, [conditions, f"price cell: {raw_price_text}"]))
        valid_raw = values.get("price_valid_until")
        valid_until = parse_date(valid_raw)
        if _clean(valid_raw) and valid_until is None:
            warnings.append(f"{reference_prefix} row {idx + 2}: unreadable validity date "
                            f"'{_clean(valid_raw)}'")
        product = Product(
            product_id=_clean(values.get("product_id")),
            product_name=_clean(values.get("product_name")),
            category=_clean(values.get("category")),
            model=_clean(values.get("model")),
            configuration=_clean(values.get("configuration")),
            description=_clean(values.get("description")),
            technical_specifications=_clean(values.get("technical_specifications")),
            applications=_clean(values.get("applications")),
            listed_price=price,
            currency=currency or None,
            tax_included=tax_included,
            tax_rate=tax_rate,
            installation_charges=_clean(values.get("installation_charges")),
            shipping_charges=_clean(values.get("shipping_charges")),
            warranty=_clean(values.get("warranty")),
            availability=_clean(values.get("availability")),
            minimum_order_quantity=_clean(values.get("minimum_order_quantity")),
            price_valid_until=valid_until,
            price_conditions=conditions,
            source_file=source_file,
            source_reference=f"{reference_prefix} row {idx + 2}",
            last_updated_at=last_updated_at,
        )
        if not (product.model or product.product_id):
            continue
        products.append(product)
    return products


def product_to_text(p: Product) -> str:
    parts = [p.display_name]
    for label, value in (("Category", p.category), ("Description", p.description),
                         ("Specifications", p.technical_specifications),
                         ("Applications", p.applications), ("Warranty", p.warranty)):
        if value:
            parts.append(f"{label}: {value}")
    return ". ".join(parts)


def chunk_text(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            chunks.append(current)
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size - overlap:]
        current = para
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------

class DocumentLoader:
    def __init__(self, default_currency: str | None = None, ocr_enabled: bool = False,
                 ocr_language: str = "eng") -> None:
        self.default_currency = default_currency
        self.ocr_enabled = ocr_enabled
        self.ocr_language = ocr_language

    def load(self, path: Path, display_name: str | None = None) -> LoadedDocument:
        name = display_name or path.name
        doc = LoadedDocument(source_file=name)
        updated = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        suffix = path.suffix.lower()
        try:
            if suffix == ".csv":
                frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="replace")
                self._load_frame(frame, doc, "csv", updated)
            elif suffix in {".xlsx", ".xlsm"}:
                sheets = pd.read_excel(path, sheet_name=None, dtype=object)
                for sheet_name, frame in sheets.items():
                    self._load_frame(frame, doc, f"sheet '{sheet_name}'", updated)
            elif suffix == ".docx":
                self._load_docx(path, doc, updated)
            elif suffix == ".pdf":
                self._load_pdf(path, doc, updated)
            elif suffix in {".txt", ".md"}:
                text = path.read_text(encoding="utf-8", errors="replace")
                for i, chunk in enumerate(chunk_text(text)):
                    doc.chunks.append((f"chunk {i + 1}", chunk))
            elif suffix in {".png", ".jpg", ".jpeg"}:
                self._load_image(path, doc)
            else:
                doc.warnings.append(f"unsupported file type {suffix}")
        except Exception as exc:  # noqa: BLE001 - report per-file, never crash ingestion
            log.exception("Failed to parse %s", name)
            doc.error = f"{type(exc).__name__}: {exc}"
        if not doc.error and not doc.products and not doc.chunks:
            doc.warnings.append("no usable content extracted")
        return doc

    def _load_frame(self, frame: pd.DataFrame, doc: LoadedDocument, ref: str, updated: str) -> None:
        frame = frame.dropna(how="all")
        headers = list(frame.columns)
        rows = frame.values.tolist()
        mapping = map_product_headers(headers)
        if is_product_table(mapping):
            products = rows_to_products(headers, rows, doc.source_file, ref,
                                        self.default_currency, updated, doc.warnings)
            doc.products.extend(products)
            for p in products:
                doc.chunks.append((p.source_reference or ref, product_to_text(p)))
        else:
            for i, row in enumerate(rows):
                text = "; ".join(f"{h}: {v}" for h, v in zip(headers, row) if _clean(v))
                if text:
                    doc.chunks.append((f"{ref} row {i + 2}", text))

    def _load_docx(self, path: Path, doc: LoadedDocument, updated: str) -> None:
        import docx  # python-docx

        document = docx.Document(str(path))
        text = "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
        for i, chunk in enumerate(chunk_text(text)):
            doc.chunks.append((f"paragraphs chunk {i + 1}", chunk))
        for t_index, table in enumerate(document.tables, start=1):
            cells = [[c.text.strip() for c in row.cells] for row in table.rows]
            if len(cells) < 2:
                continue
            self._load_table(cells[0], cells[1:], doc, f"table {t_index}", updated)

    def _load_table(self, headers: list[Any], rows: list[list[Any]], doc: LoadedDocument,
                    ref: str, updated: str) -> None:
        mapping = map_product_headers(headers)
        if is_product_table(mapping):
            products = rows_to_products(headers, rows, doc.source_file, ref,
                                        self.default_currency, updated, doc.warnings)
            doc.products.extend(products)
            for p in products:
                doc.chunks.append((p.source_reference or ref, product_to_text(p)))
        else:
            text = "\n".join(" | ".join(str(c) for c in row if c) for row in [headers, *rows])
            for i, chunk in enumerate(chunk_text(text)):
                doc.chunks.append((f"{ref} chunk {i + 1}", chunk))

    def _load_pdf(self, path: Path, doc: LoadedDocument, updated: str) -> None:
        scanned_pages: list[int] = []
        try:
            import pdfplumber
        except ImportError:  # pragma: no cover
            pdfplumber = None  # type: ignore[assignment]

        if pdfplumber is not None:
            with pdfplumber.open(str(path)) as pdf:
                for number, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    if len(text.strip()) < 20:
                        scanned_pages.append(number)
                        continue
                    for t_index, table in enumerate(page.extract_tables() or [], start=1):
                        if table and len(table) >= 2:
                            self._load_table(table[0], table[1:], doc,
                                             f"page {number} table {t_index}", updated)
                    for i, chunk in enumerate(chunk_text(text)):
                        doc.chunks.append((f"page {number} chunk {i + 1}", chunk))
        else:  # pragma: no cover
            from pypdf import PdfReader

            for number, page in enumerate(PdfReader(str(path)).pages, start=1):
                text = page.extract_text() or ""
                if len(text.strip()) < 20:
                    scanned_pages.append(number)
                    continue
                for i, chunk in enumerate(chunk_text(text)):
                    doc.chunks.append((f"page {number} chunk {i + 1}", chunk))

        if scanned_pages:
            if self.ocr_enabled:
                self._ocr_pdf_pages(path, scanned_pages, doc)
            else:
                doc.warnings.append(
                    f"page(s) {scanned_pages} contain no extractable text (scanned?). "
                    "Set OCR_ENABLED=true (requires tesseract) to OCR them.")

    def _ocr_pdf_pages(self, path: Path, pages: list[int], doc: LoadedDocument) -> None:
        try:
            import pypdfium2 as pdfium
            import pytesseract
        except ImportError as exc:
            doc.warnings.append(f"OCR requested but unavailable: {exc}")
            return
        pdf = pdfium.PdfDocument(str(path))
        for number in pages:
            image = pdf[number - 1].render(scale=2).to_pil()
            text = pytesseract.image_to_string(image, lang=self.ocr_language)
            if text.strip():
                for i, chunk in enumerate(chunk_text(text)):
                    doc.chunks.append((f"page {number} (OCR) chunk {i + 1}", chunk))
                # OCR'd prices are never turned into structured, quotable prices.
                doc.warnings.append(f"page {number} was OCR'd; OCR text is used for retrieval "
                                    "only and never as a quotable price")
            else:
                doc.warnings.append(f"page {number}: OCR produced no text")

    def _load_image(self, path: Path, doc: LoadedDocument) -> None:
        if not self.ocr_enabled:
            doc.warnings.append("image file skipped; enable OCR_ENABLED to extract its text")
            return
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            doc.warnings.append(f"OCR requested but unavailable: {exc}")
            return
        text = pytesseract.image_to_string(Image.open(path), lang=self.ocr_language)
        for i, chunk in enumerate(chunk_text(text)):
            doc.chunks.append((f"image OCR chunk {i + 1}", chunk))
        doc.warnings.append("image was OCR'd; text used for retrieval only")
