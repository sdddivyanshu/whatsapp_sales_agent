"""Catalogue parsing across file formats, including scanned-PDF detection."""

from __future__ import annotations

import pytest

from app.knowledge.document_loader import DocumentLoader

HEADERS = ["Model No", "Product", "Category", "Price (INR)", "GST", "Valid Till"]
ROW = ["XL-100", "Test Wirecut", "EDM Wirecut", "₹ 9,50,000", "Extra", "31/03/2027"]


def test_xlsx_price_list(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(HEADERS)
    ws.append(ROW)
    ws.append(["", "", "", "", "", ""])
    path = tmp_path / "prices.xlsx"
    wb.save(path)
    doc = DocumentLoader().load(path)
    assert doc.error is None
    [p] = doc.products
    assert (p.model, p.listed_price, p.currency, p.tax_included, p.price_valid_until) == \
        ("XL-100", 950000.0, "INR", False, "2027-03-31")
    assert p.source_reference == "sheet 'Prices' row 2"


def test_docx_table_and_text(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("XL-100 is ideal for precision dies. Installation needs 3-phase power.")
    table = document.add_table(rows=2, cols=len(HEADERS))
    for i, h in enumerate(HEADERS):
        table.rows[0].cells[i].text = h
        table.rows[1].cells[i].text = ROW[i]
    path = tmp_path / "catalogue.docx"
    document.save(path)
    doc = DocumentLoader().load(path)
    assert [p.model for p in doc.products] == ["XL-100"]
    assert any("precision dies" in text for _, text in doc.chunks)


def test_pdf_text_and_scanned_page_detection(tmp_path):
    canvas_mod = pytest.importorskip("reportlab.pdfgen.canvas")
    path = tmp_path / "brochure.pdf"
    c = canvas_mod.Canvas(str(path))
    c.drawString(72, 720, "XL-100 wire cut EDM brochure: suitable for carbide and die steel jobs.")
    c.showPage()
    c.rect(100, 100, 200, 200, fill=1)               # an image-only (scanned-like) page
    c.showPage()
    c.save()
    doc = DocumentLoader(ocr_enabled=False).load(path)
    assert any("carbide" in text for _, text in doc.chunks)
    assert doc.status == "WARNING" and any("OCR_ENABLED" in w for w in doc.warnings)
    assert doc.products == []                         # brochures never create quotable prices


def test_text_file_and_unknown_values(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Warranty: 12 months on all EDM machines.\n\nService engineers in Pune.", encoding="utf-8")
    doc = DocumentLoader().load(path)
    assert len(doc.chunks) == 1 and doc.products == []

    csv_path = tmp_path / "partial.csv"
    csv_path.write_text("model,product_name,price\nPX-1,Partial,\n", encoding="utf-8")
    [p] = DocumentLoader(default_currency="INR").load(csv_path).products
    assert p.listed_price is None and p.currency is None and p.tax_included is None and p.warranty is None
