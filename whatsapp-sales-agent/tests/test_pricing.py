"""Catalogue ingestion, exact price lookup and the no-fabrication guarantees."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import inbound, start_lead

from app.agents.guardrails import check_reply, extract_amounts
from app.agents.mock_responses import mock_respond
from app.agents.sales_agent import RESPOND_TOOL
from app.integrations.anthropic_client import MockLLMClient
from app.knowledge.document_loader import parse_price, parse_tax_included
from app.knowledge.price_lookup import PriceLookup, PriceStatus

PRICE_HEADER = "product_id,product_name,category,model,configuration,price,currency,gst,price_valid_until\n"


def write_kb(app, name: str, body: str) -> None:
    (app.settings.knowledge_base_dir / name).write_text(PRICE_HEADER + body, encoding="utf-8")
    app.refresh_knowledge_base()


def test_exact_model_lookup_returns_configured_price(app):
    check = app.price_lookup.check(app.price_lookup.match_models("price of WC-400?")[0], "price of WC-400?")
    assert check.status == PriceStatus.VERIFIED
    assert check.product.listed_price == 1250000 and check.product.currency == "INR"
    assert check.product.tax_included is False and check.product.tax_rate == 18
    assert check.product.source_file == "sample_price_list.csv"


@pytest.mark.parametrize("text,expected", [
    ("price of WC-400", ["WC400"]),
    ("price of WC 400S", ["WC400S"]),
    ("WC-400 S please", ["WC400S"]),
    ("compare wc400 and wc400s", ["WC400", "WC400S"]),
    ("WC-4000 price", []),
    ("WC price", []),
])
def test_similar_models_are_not_confused(app, text, expected):
    assert sorted(app.price_lookup.match_models(text)) == sorted(expected)


def test_unknown_model_does_not_produce_a_price(app):
    start_lead(app, lead_id="U1", phone="+919876500001")
    inbound(app, "+919876500001", "What is the price of model XT-9000?")
    reply = app.db.query_one("SELECT body FROM messages WHERE lead_id='U1' AND direction='outbound' "
                             "ORDER BY id DESC LIMIT 1")["body"]
    assert extract_amounts(reply) == []
    assert app.db.query("SELECT * FROM quotes_audit") == []


def test_model_without_numeric_price_is_not_quoted(app):
    check = app.price_lookup.check("SP35")
    assert check.status == PriceStatus.NO_PRICE and not check.quotable


def test_missing_tax_information_is_not_assumed(app):
    write_kb(app, "extra.csv", "X1,Test Machine,EDM Drill,TX-100,,500000,INR,,2027-12-31\n")
    check = app.price_lookup.check("TX100")
    assert check.status == PriceStatus.VERIFIED
    assert check.product.tax_included is None
    assert "not specified" in check.product.tax_treatment()
    assert check.as_fact()["tax"] == "GST treatment not specified in price list"


def test_conflicting_price_entries_trigger_review(app):
    write_kb(app, "old_list.csv", "SAMPLE-WC400,EDM Wirecut Machine,EDM Wirecut,WC-400,Standard,1190000,INR,Extra,2027-03-31\n")
    check = app.price_lookup.check("WC400")
    assert check.status == PriceStatus.CONFLICT and not check.quotable

    start_lead(app, lead_id="C1", phone="+919876500001")
    inbound(app, "+919876500001", "Price of WC-400 please")
    reply = app.db.query_one("SELECT body FROM messages WHERE lead_id='C1' AND direction='outbound' "
                             "ORDER BY id DESC LIMIT 1")["body"]
    assert extract_amounts(reply) == []
    conv = app.db.query_one("SELECT * FROM conversations WHERE lead_id='C1'")
    assert conv["automation_paused"] == 1
    lead = app.db.query_one("SELECT status FROM leads WHERE lead_id='C1'")
    assert lead["status"] == "HUMAN_HANDOVER"
    from app.repositories.database import loads
    assert loads(conv["memory_json"])["qualification"]["classification"] == "NEEDS_HUMAN_REVIEW"


def test_multiple_configurations_require_clarification(app):
    assert app.price_lookup.check("SP50", "price of SP-50").status == PriceStatus.AMBIGUOUS
    znc = app.price_lookup.check("SP50", "price of SP-50 ZNC")
    assert znc.status == PriceStatus.VERIFIED and znc.product.listed_price == 890000


def test_expired_and_stale_prices_are_not_quotable(app):
    expired = PriceLookup(app.products, today=lambda: date(2027, 4, 1)).check("WC400")
    assert expired.status == PriceStatus.EXPIRED
    write_kb(app, "novalidity.csv", "N1,Machine,EDM,NV-1,,100000,INR,Extra,\n")
    stale = PriceLookup(app.products, price_max_age_days=10,
                        today=lambda: date.today().replace(year=date.today().year + 1)).check("NV1")
    assert stale.status == PriceStatus.STALE


def test_reingestion_is_idempotent_and_removes_deleted_files(app):
    before = len(app.products.all_products())
    report = app.refresh_knowledge_base()
    assert report.ingested == [] and len(app.products.all_products()) == before
    write_kb(app, "temp.csv", "T1,Temp,EDM,TMP-1,,1000,INR,Extra,2027-01-01\n")
    assert app.price_lookup.check("TMP1").quotable
    (app.settings.knowledge_base_dir / "temp.csv").unlink()
    report = app.refresh_knowledge_base()
    assert report.removed == ["temp.csv"]
    assert app.price_lookup.check("TMP1").status == PriceStatus.NOT_FOUND


def test_hallucinated_price_from_model_is_blocked(make_app):
    llm = MockLLMClient()

    def lying_model(system, messages, ctx):
        out = mock_respond(system, messages, ctx)
        out["reply_text"] = "The WC-400 is just ₹9,99,000 today, and we can offer 10% off!"
        return out

    llm.register(RESPOND_TOOL, lying_model)
    app = make_app(llm=llm)
    start_lead(app, lead_id="H1", phone="+919876500001")
    inbound(app, "+919876500001", "price of WC-400?")
    reply = app.db.query_one("SELECT body, meta_json FROM messages WHERE lead_id='H1' AND "
                             "direction='outbound' ORDER BY id DESC LIMIT 1")
    assert "9,99,000" not in reply["body"] and extract_amounts(reply["body"]) == []
    assert "unverified amount" in reply["meta_json"]
    assert app.db.query("SELECT * FROM quotes_audit") == []
    events = [a["event"] for a in app.db.query("SELECT event FROM audit_log")]
    assert "reply_guard_triggered" in events


def test_verified_quote_is_audited_with_source(app):
    start_lead(app, lead_id="Q1", phone="+919876500001")
    inbound(app, "+919876500001", "What is the price of DR-20?")
    quote = app.db.query_one("SELECT * FROM quotes_audit WHERE lead_id='Q1'")
    assert quote["model"] == "DR-20" and quote["listed_price"] == 385000
    assert quote["source_file"] == "sample_price_list.csv" and quote["source_reference"]
    assert quote["tax_included"] == 0


@pytest.mark.parametrize("cell,expected", [
    ("₹ 12,50,000", 1250000), ("Rs. 1250000/-", 1250000), ("12.5 lakh", 1250000),
    ("INR 12,50,000 + GST 18%", 1250000), ("On request", None), ("10-12 lakh", None), ("", None),
])
def test_price_parsing(cell, expected):
    assert parse_price(cell) == expected


def test_tax_parsing_never_guesses():
    assert parse_tax_included("Extra") is False
    assert parse_tax_included("Inclusive of GST") is True
    assert parse_tax_included("") is None
    assert parse_tax_included("see terms") is None


def test_guard_allows_only_verified_amounts_and_official_contacts():
    ok = check_reply("WC-400 is ₹12,50,000 + GST. Call +91 90000 00001.", allowed_amounts={1250000.0},
                     allowed_phones=["+919000000001"], allowed_emails=[], secrets=[], fallback="FB")
    assert ok.passed
    bad = check_reply("Call our other customer at +91 98765 11111", allowed_amounts=set(),
                      allowed_phones=["+919000000001"], allowed_emails=[], secrets=[], fallback="FB")
    assert not bad.passed and bad.text == "FB"
