"""Send a correctly signed fake customer message to a RUNNING server (local testing).

    python scripts/simulate_inbound.py --phone +919876543210 --text "Price of WC-400?"
    python scripts/simulate_inbound.py --url https://<tunnel>/webhook/whatsapp ...

Uses WHATSAPP_APP_SECRET and WHATSAPP_PHONE_NUMBER_ID from .env. Only the local
server is contacted; nothing is sent to WhatsApp unless the server is in live mode.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.integrations.whatsapp_cloud import sign_body  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000/webhook/whatsapp")
    parser.add_argument("--phone", required=True, help="customer number in E.164, e.g. +919876543210")
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    s = load_settings()
    wa_id = args.phone.lstrip("+")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "SIM", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "", "phone_number_id": s.whatsapp_phone_number_id or "SIM"},
            "contacts": [{"profile": {"name": "Simulated"}, "wa_id": wa_id}],
            "messages": [{"from": wa_id, "id": f"wamid.SIM{uuid.uuid4().hex[:16]}",
                          "timestamp": str(int(time.time())), "type": "text", "text": {"body": args.text}}],
        }}]}]}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if s.whatsapp_app_secret:
        headers["X-Hub-Signature-256"] = sign_body(s.whatsapp_app_secret, body)
    req = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - local URL by default
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    main()
