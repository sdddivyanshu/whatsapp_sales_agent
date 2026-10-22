"""Lead detection, deduplication and CSV safety."""

from __future__ import annotations

import os
import time

from conftest import HEADER, append_csv, read_csv, write_csv

from app.ingestion.csv_file import read_csv_document
from app.ingestion.csv_monitor import JOB_CONTACT_LEAD, assign_row_identities, input_hash
from app.models.lead import OUTPUT_COLUMNS


def _contact_jobs(app) -> list:
    return app.db.query("SELECT * FROM jobs WHERE job_type=?", (JOB_CONTACT_LEAD,))


def test_new_row_triggers_exactly_one_workflow(app):
    path = app.settings.leads_csv_path
    write_csv(path, [["L001", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app.csv_monitor.initialize()                      # existing row -> baseline, not contacted
    assert _contact_jobs(app) == []
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='L001'")["status"] == "SKIPPED_EXISTING"

    append_csv(path, [["L002", "Priya", "XYZ", "+919876543211", "EDM Drill", "OPTED_IN"]])
    result = app.csv_monitor.scan(force=True)
    assert result.new == ["L002"]
    app.csv_monitor.scan(force=True)                  # rescanning the same content is a no-op
    assert len(_contact_jobs(app)) == 1
    app.worker.drain()
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='L002'")["status"] == "MESSAGE_SENT"
    sent = app.provider.sent
    assert len(sent) == 1 and sent[0]["kind"] == "template" and sent[0]["to"] == "+919876543211"


def test_existing_rows_do_not_trigger_after_restart(make_app, settings):
    app = make_app()
    write_csv(settings.leads_csv_path, [])
    app.csv_monitor.initialize()
    append_csv(settings.leads_csv_path, [["L010", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    app.worker.drain()
    assert len(app.provider.sent) == 1

    restarted = make_app()                            # same database, new process
    restarted.csv_monitor.initialize()
    result = restarted.csv_monitor.scan(force=True)
    restarted.worker.drain()
    assert result.new == []
    assert restarted.provider.sent == []
    assert len(_contact_jobs(restarted)) == 1


def test_process_existing_on_startup_when_configured(make_app, settings):
    settings.csv_process_existing_on_startup = True
    write_csv(settings.leads_csv_path, [["L1", "A", "B", "+919876543210", "EDM Drill", "OPTED_IN"]])
    app = make_app()
    app.csv_monitor.initialize()
    result = app.csv_monitor.scan(force=True)
    assert result.new == ["L1"]


def test_updating_summary_columns_does_not_create_new_lead(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    append_csv(path, [["L003", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    app.worker.drain()                                # sends intro and writes status columns
    rows = read_csv(path)
    assert rows[0]["agent_status"] == "MESSAGE_SENT"
    hash_before = app.db.query_one("SELECT input_hash FROM leads WHERE lead_id='L003'")["input_hash"]

    # A person edits an output column by hand as well.
    doc = read_csv_document(path)
    doc.rows[0]["next_action"] = "edited by hand"
    from app.ingestion.csv_file import atomic_write_document
    atomic_write_document(path, doc)

    result = app.csv_monitor.scan(force=True)
    assert result.new == [] and result.updated == []
    assert app.db.query_one("SELECT input_hash FROM leads WHERE lead_id='L003'")["input_hash"] == hash_before
    assert len(_contact_jobs(app)) == 1


def test_edited_input_row_is_update_not_new_lead(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    append_csv(path, [["L004", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    write_csv(path, [["L004", "Rahul S", "ABC", "+919876543210", "EDM Wirecut 400", "OPTED_IN"]])
    result = app.csv_monitor.scan(force=True)
    assert result.updated == ["L004"] and result.new == [] and result.reprocessed == []
    assert app.db.query_one("SELECT requirement FROM leads WHERE lead_id='L004'")["requirement"] == "EDM Wirecut 400"
    audit = app.db.query("SELECT event FROM audit_log WHERE lead_id='L004'")
    assert "lead_input_changed" in [a["event"] for a in audit]


def test_rows_without_lead_id_get_stable_generated_ids(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    append_csv(path, [["", "Rahul", "ABC", "98765 43210", "EDM Wirecut", "OPTED_IN"],
                      ["", "Rahul", "ABC", "98765 43210", "EDM Wirecut", "OPTED_IN"]])  # identical rows
    result = app.csv_monitor.scan(force=True)
    assert len(result.new) == 2 and all(i.startswith("AUTO-") for i in result.new)
    ids_in_file = [r["lead_id"] for r in read_csv(path)]
    assert ids_in_file == result.new                  # written back for future identity
    again = app.csv_monitor.scan(force=True)
    assert again.new == []
    # Identity function is deterministic
    rows = [{"mobile_number": "98765 43210", "customer_name": "Rahul", "company_name": "ABC",
             "requirement": "EDM Wirecut"}] * 2
    assert [i for i, _ in assign_row_identities(rows)] == result.new


def test_duplicate_looking_lead_is_flagged_not_dropped(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    append_csv(path, [["D1", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"],
                      ["D2", "Rahul S.", "ABC Tools", "9876543210", "edm wirecut", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    app.worker.drain()
    d2 = app.db.query_one("SELECT * FROM leads WHERE lead_id='D2'")
    assert d2["status"] == "DUPLICATE_REVIEW" and d2["duplicate_of"] == "D1"
    assert len(app.provider.sent) == 1
    events = [a["event"] for a in app.db.query("SELECT event FROM audit_log WHERE lead_id='D2'")]
    assert "duplicate_detected" in events
    # Operator approves the duplicate -> it is contacted.
    app.outreach.retry_contact("D2", approve_duplicate=True)
    app.worker.drain()
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='D2'")["status"] == "MESSAGE_SENT"


def test_messy_csv_is_handled(app):
    path = app.settings.leads_csv_path
    content = (
        "﻿Lead ID,Customer Name,Company Name,Mobile Number,Requirement,Consent Status,Mobile\n"
        "M1,Rahul,ABC,+919876543210,EDM Wirecut,OPTED_IN,x\n"
        "\n"
        ",,,,,,\n"
        "Lead ID,Customer Name,Company Name,Mobile Number,Requirement,Consent Status,Mobile\n"
        "M2,Priya,XYZ,9876543211,EDM Drill,OPTED_IN,y,EXTRA,FIELDS\n"
        "M3,Anil,PQR,9876543212,EDM Sparking\n"
    )
    path.write_text(content, encoding="utf-8")
    doc = read_csv_document(path)
    assert doc.columns[:6] == ["lead_id", "customer_name", "company_name", "mobile_number",
                               "requirement", "consent_status"]
    assert [r["lead_id"] for r in doc.rows] == ["M1", "M3"]
    assert any("duplicate column" in w for w in doc.warnings)
    assert any("repeated header" in w for w in doc.warnings)
    assert any("malformed" in w for w in doc.warnings)
    assert doc.rows[1]["consent_status"] == ""        # short row padded


def test_non_utf8_file_is_decoded(app):
    path = app.settings.leads_csv_path
    path.write_bytes(",".join(HEADER).encode() + b"\nC1,Jos\xe9,ABC,+919876543210,EDM Drill,OPTED_IN\n")
    doc = read_csv_document(path)
    assert doc.rows[0]["customer_name"] == "José"


def test_incomplete_write_is_retried_later(app):
    from app.ingestion.csv_file import CsvNotReady

    path = app.settings.leads_csv_path
    path.write_text(",".join(HEADER) + "\nI1,Rahul,ABC,+9198765", encoding="utf-8")  # no newline yet
    try:
        read_csv_document(path, settle_seconds=5)
        raise AssertionError("expected CsvNotReady")
    except CsvNotReady:
        pass


def test_atomic_rename_save_is_detected(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    app.csv_monitor.scan(force=True)
    tmp = path.with_name(".leads.csv.tmp")
    write_csv(tmp, [["R1", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    time.sleep(0.01)
    os.replace(tmp, path)                             # editor-style save
    result = app.csv_monitor.scan()                   # no force: signature (inode/mtime) changed
    assert result is not None and result.new == ["R1"]


def test_csv_writeback_preserves_all_columns_and_rows(app):
    path = app.settings.leads_csv_path
    header = HEADER + ["city", "Sales Notes"]
    write_csv(path, [["K1", "Old", "Co", "+919876543219", "EDM Drill", "OPTED_IN", "Pune", "keep me"]],
              header=header)
    app.csv_monitor.initialize()
    append_csv(path, [["K2", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN", "Rajkot", "note 2"]])
    app.csv_monitor.scan(force=True)
    app.worker.drain()
    rows = read_csv(path)
    assert [r["lead_id"] for r in rows] == ["K1", "K2"]
    assert rows[0]["Sales Notes"] == "keep me" and rows[1]["Sales Notes"] == "note 2"
    assert rows[1]["city"] == "Rajkot"
    assert set(OUTPUT_COLUMNS) <= set(rows[0].keys())
    assert rows[1]["agent_status"] == "MESSAGE_SENT"
    assert rows[0]["agent_status"] == ""              # untouched lead keeps empty outputs
    assert list(rows[0].keys())[:len(header)] == header


def test_concurrent_external_edit_is_preserved_on_writeback(app):
    path = app.settings.leads_csv_path
    write_csv(path, [])
    app.csv_monitor.initialize()
    append_csv(path, [["W1", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app.csv_monitor.scan(force=True)
    # Someone appends a row before the worker writes back the status.
    append_csv(path, [["W2", "Priya", "XYZ", "+919876543211", "EDM Drill", "OPTED_IN"]])
    app.worker.drain()
    ids = [r["lead_id"] for r in read_csv(path)]
    assert ids == ["W1", "W2"]
    result = app.csv_monitor.scan(force=True)         # W2 is still picked up as new
    assert result.new == ["W2"]


def test_input_hash_ignores_output_columns():
    base = {"lead_id": "X", "customer_name": "A", "mobile_number": "1", "requirement": "r"}
    assert input_hash(base) == input_hash({**base, "agent_status": "CLOSED", "lead_id": "Y"})
    assert input_hash(base) != input_hash({**base, "requirement": "other"})


def test_baseline_captures_freshly_written_file_at_startup(make_app, settings):
    """Regression: a file modified just before start must still be baselined, not contacted."""
    settings.csv_settle_seconds = 5
    write_csv(settings.leads_csv_path, [["F1", "Rahul", "ABC", "+919876543210", "EDM Wirecut", "OPTED_IN"]])
    app = make_app()
    app.csv_monitor.initialize()
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='F1'")["status"] == "SKIPPED_EXISTING"
    assert app.db.get_meta(app.csv_monitor.baseline_key) is not None
