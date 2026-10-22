"""Phone numbers, consent and do-not-contact handling at contact time."""

from __future__ import annotations

import pytest
from conftest import read_csv, start_lead

from app.ingestion.lead_validator import normalize_phone, validate_lead
from app.models.lead import LeadInput, LeadState


@pytest.mark.parametrize("raw,expected", [
    ("+919876543210", "+919876543210"),
    ("98765 43210", "+919876543210"),
    ("098765-43210", "+919876543210"),
    ("91 98765 43210", "+919876543210"),
    ("+91-98765-43210", "+919876543210"),
    ("0091 9876543210", "+919876543210"),
    ("+14155550123", "+14155550123"),
    ("9876543210.0", "+919876543210"),
])
def test_valid_numbers_normalise_to_e164(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "12345", "abc9876543210", "9.87654321E+09", "+91 12345 67890",  # Indian mobiles start 6-9
    "+0123456789", "98765", "+9198765432101234567",
])
def test_invalid_numbers_are_rejected(raw):
    assert normalize_phone(raw) is None


def test_invalid_mobile_lead_is_not_contacted(app):
    start_lead(app, lead_id="BAD1", phone="12345")
    lead = app.db.query_one("SELECT * FROM leads WHERE lead_id='BAD1'")
    assert lead["status"] == LeadState.INVALID_LEAD.value
    assert "mobile_number" in lead["last_error"]
    assert app.provider.sent == []
    row = read_csv(app.settings.leads_csv_path)[0]
    assert row["agent_status"] == "INVALID_LEAD" and "Fix lead data" in row["next_action"]


@pytest.mark.parametrize("consent", ["", "NO", "pending"])
def test_lead_without_permission_is_not_contacted(app, consent):
    start_lead(app, lead_id="NC1", consent=consent)
    lead = app.db.query_one("SELECT * FROM leads WHERE lead_id='NC1'")
    assert lead["status"] == LeadState.CONSENT_REQUIRED.value
    assert app.provider.sent == []
    assert read_csv(app.settings.leads_csv_path)[0]["follow_up_required"] == "Yes"


def test_consent_check_can_be_disabled_by_configuration(make_app, settings):
    settings.consent_required = False
    app = make_app()
    start_lead(app, lead_id="NC2", consent="")
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='NC2'")["status"] == "MESSAGE_SENT"


def test_number_on_dnc_list_is_never_contacted(app):
    app.outreach.leads.add_dnc("+919876500001", "opted out earlier")
    start_lead(app, lead_id="DNC1", phone="+919876500001")
    assert app.db.query_one("SELECT status FROM leads WHERE lead_id='DNC1'")["status"] == "DO_NOT_CONTACT"
    assert app.provider.sent == []


def test_validate_lead_pure_function():
    ok = validate_lead(LeadInput(mobile_number="9876543210", requirement="EDM", consent_status="opted_in"),
                       default_country_code="91", consent_required=True,
                       consent_accepted_values=["OPTED_IN"])
    assert ok.valid and ok.mobile_e164 == "+919876543210"
    missing = validate_lead(LeadInput(mobile_number="9876543210"), default_country_code="91",
                            consent_required=False, consent_accepted_values=[])
    assert not missing.valid and missing.state == LeadState.INVALID_LEAD
