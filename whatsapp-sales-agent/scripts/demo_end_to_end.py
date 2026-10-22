"""Offline end-to-end demonstration (mock WhatsApp + mock LLM, nothing is sent).

    python scripts/demo_end_to_end.py            # uses a temp working folder
    python scripts/demo_end_to_end.py --keep DIR  # keep the generated files in DIR

Steps: ingest the sample catalogue -> start with an existing CSV (baseline) ->
append a new lead -> intro template "sent" -> simulated customer replies ->
agent answers with verified prices -> customer finishes -> summary -> CSV updated.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.models.message import InboundMessage, ParsedWebhook  # noqa: E402
from app.services.container import AppContainer  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", help="directory to keep the demo files in")
    args = parser.parse_args()
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="wa-agent-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    kb = work / "knowledge_base"
    shutil.copytree(ROOT / "data" / "knowledge_base", kb, dirs_exist_ok=True)
    leads_csv = work / "leads.csv"
    shutil.copy(ROOT / "data" / "leads.example.csv", leads_csv)

    import os

    os.environ.update({
        "WHATSAPP_MODE": "mock", "LLM_MODE": "mock", "DATABASE_PATH": str(work / "agent.db"),
        "LEADS_CSV_PATH": str(leads_csv), "KNOWLEDGE_BASE_DIR": str(kb), "CSV_SETTLE_SECONDS": "0",
        "BUSINESS_PROFILE_PATH": str(ROOT / "config" / "business_profile.demo.yaml"),
        "OUTPUT_SINKS": "csv", "WHATSAPP_APP_SECRET": "", "ADMIN_API_KEY": "",
    })
    settings = load_settings(env_file=str(work / ".env-none"))
    app = AppContainer(settings)
    report = app.refresh_knowledge_base()
    print(f"Catalogue: ingested {report.ingested}, products={len(app.products.all_products())}")

    app.csv_monitor.initialize()  # existing rows L001/L002 are baselined, not contacted
    print("Existing rows baselined (not contacted).")

    with leads_csv.open("a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["", "Anil Deshmukh", "Precision Dies Pvt Ltd", "98220 12345",
                                 "EDM Wirecut machine for press tool dies", "Pune", "Maharashtra",
                                 "Tool room", "OPTED_IN", "website"])
    result = app.csv_monitor.scan()
    print(f"New leads detected: {result.new if result else []}")
    app.worker.drain()
    lead_id = result.new[0]
    phone = "+919822012345"

    def customer(text: str) -> None:
        print(f"\nCUSTOMER: {text}")
        msg = InboundMessage(wa_message_id=f"wamid.DEMO{uuid.uuid4().hex[:12]}", from_phone=phone,
                             msg_type="text", text=text)
        app.conversations.handle_webhook(ParsedWebhook(messages=[msg]))
        app.worker.drain()
        last = app.db.query_one("SELECT body FROM messages WHERE lead_id=? AND direction='outbound' "
                                "ORDER BY id DESC LIMIT 1", (lead_id,))
        print(f"AGENT:    {last['body']}")

    intro = app.db.query_one("SELECT body FROM messages WHERE lead_id=? ORDER BY id LIMIT 1", (lead_id,))
    print(f"\nAGENT (template): {intro['body']}")
    customer("Hi, we make press tool dies from D2 die steel, up to 250 mm thick.")
    customer("What is the price of WC-400 and WC-400S?")
    customer("We are planning to buy within 2 months for capacity expansion. Please send a quotation.")
    customer("Thanks, that's all for now")

    lead = app.db.query_one("SELECT status FROM leads WHERE lead_id=?", (lead_id,))
    summary = app.db.query_one("SELECT summary_json FROM summaries WHERE lead_id=?", (lead_id,))
    print(f"\nFinal lead status: {lead['status']}")
    print("Summary JSON:\n" + json.dumps(json.loads(summary["summary_json"]), indent=2, ensure_ascii=False))
    print("\nUpdated CSV row:")
    with leads_csv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["lead_id"] == lead_id:
                for k, v in row.items():
                    print(f"  {k}: {v}")
    print(f"\nDemo files: {work}")


if __name__ == "__main__":
    main()
