"""Opt-in integration tests against REAL services.

Skipped unless RUN_LIVE_TESTS=1 and the relevant credentials are set:

    RUN_LIVE_TESTS=1 ANTHROPIC_API_KEY=... pytest -m live tests/integration
    RUN_LIVE_TESTS=1 WHATSAPP_ACCESS_TOKEN=... WHATSAPP_PHONE_NUMBER_ID=... \
        LIVE_TEST_RECIPIENT=+91XXXXXXXXXX WHATSAPP_TEMPLATE_NAME=hello_world \
        WHATSAPP_TEMPLATE_LANGUAGE=en_US WHATSAPP_TEMPLATE_PARAMS= pytest -m live tests/integration

The WhatsApp test sends ONE template message to LIVE_TEST_RECIPIENT, which must be
a number you own and that is allowed for your app (test numbers in development mode).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS != 1")]


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
def test_claude_returns_structured_reply(settings):
    from app.config import load_business_profile
    from app.integrations.anthropic_client import AnthropicClient
    from app.knowledge.price_lookup import PriceLookup
    from app.knowledge.product_repository import ProductRepository
    from app.knowledge.document_loader import DocumentLoader
    from app.knowledge.retrieval import BM25Retriever
    from app.agents.sales_agent import AgentTurn, SalesAgent
    from app.agents.guardrails import extract_amounts
    from app.repositories.database import Database

    db = Database(settings.database_path)
    db.init()
    repo = ProductRepository(db, DocumentLoader(default_currency="INR"))
    repo.ingest_directory(settings.knowledge_base_dir)
    llm = AnthropicClient(os.environ["ANTHROPIC_API_KEY"],
                          os.environ.get("ANTHROPIC_MODEL", settings.anthropic_model))
    agent = SalesAgent(llm, load_business_profile(settings), PriceLookup(repo), BM25Retriever(db))
    result = agent.respond(AgentTurn(lead={"customer_name": "Rahul", "requirement": "EDM Wirecut"},
                                     memory={}, history=[{"role": "agent", "text": "Hello Rahul!"}],
                                     new_messages=["What is the price of WC-400 and XT-9000?"]))
    assert result.decision.reply_text
    assert set(extract_amounts(result.decision.reply_text)) <= {1250000.0}


@pytest.mark.skipif(not (os.environ.get("WHATSAPP_ACCESS_TOKEN") and os.environ.get("LIVE_TEST_RECIPIENT")),
                    reason="WhatsApp live credentials / LIVE_TEST_RECIPIENT not set")
def test_whatsapp_template_send():
    from app.integrations.whatsapp_cloud import WhatsAppCloudClient

    client = WhatsAppCloudClient(access_token=os.environ["WHATSAPP_ACCESS_TOKEN"],
                                 phone_number_id=os.environ["WHATSAPP_PHONE_NUMBER_ID"],
                                 api_version=os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v26.0"))
    params = [p for p in os.environ.get("LIVE_TEST_TEMPLATE_VALUES", "").split("|") if p]
    result = client.send_template(os.environ["LIVE_TEST_RECIPIENT"],
                                  os.environ.get("WHATSAPP_TEMPLATE_NAME", "hello_world"),
                                  os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "en_US"), params)
    assert result.wa_message_id.startswith("wamid.")
