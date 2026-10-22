# WhatsApp AI Sales & Lead-Qualification Agent

An auditable sales assistant for an industrial-machinery business. It watches a lead CSV,
sends an approved WhatsApp introduction through the **official Meta WhatsApp Cloud API**,
answers questions with **Claude** grounded in your catalogue and price list, qualifies the
lead with transparent rules, hands over to a salesperson when needed, and writes a factual
summary back to the CSV (and optionally Google Sheets).

```
leads.csv ──► CSV monitor ──► CONTACT_LEAD job ──► validate (E.164, consent, DNC, duplicates)
                                                   └► approved template ──► WhatsApp Cloud API
Meta webhook ──► signature check ──► store message ──► PROCESS_CONVERSATION job
      rules (opt-out / discount / human / complaint) + exact price lookup + catalogue retrieval
      ──► Claude (structured tool call) ──► guardrails (prices, contacts, leaks) ──► reply
closure (finished / opt-out / inactivity / handover / operator) ──► summary (once per closure)
      ──► SYNC_OUTPUT jobs ──► CSV row update  +  Google Sheets row update (independent retries)
```

Everything durable lives in SQLite (leads, conversations, messages, jobs, quotes audit,
summaries, sync state), so restarts resume exactly where they stopped.

---

## 1. Repository structure

```
whatsapp-sales-agent/
├── app/
│   ├── main.py                    FastAPI app (webhook, health, admin) + background threads
│   ├── config.py                  Settings from env; business profile loader
│   ├── logging_utils.py           Phone masking + secret redaction in logs
│   ├── api/                       health.py, whatsapp_webhook.py, admin.py
│   ├── agents/
│   │   ├── sales_agent.py         Retrieval → Claude → overrides → guardrails
│   │   ├── system_prompt.py       Sales + summary system prompts
│   │   ├── intent_rules.py        Deterministic opt-out / discount / human / complaint detection
│   │   ├── guardrails.py          Blocks unverified amounts, discount promises, foreign contacts, leaks
│   │   ├── qualification.py       Configurable HOT/WARM/COLD/IRRELEVANT/NEEDS_HUMAN_REVIEW scoring
│   │   ├── summarizer.py          Closure summary (LLM narrative + system-verified facts)
│   │   └── mock_responses.py      Offline fixture "LLM" for mock mode and tests
│   ├── integrations/              whatsapp_cloud.py, anthropic_client.py, google_sheets.py
│   ├── ingestion/                 csv_file.py (safe read/write), csv_monitor.py, lead_validator.py
│   ├── knowledge/                 document_loader.py, product_repository.py, price_lookup.py, retrieval.py
│   ├── services/                  workflow.py (state machine), message_queue.py (durable jobs),
│   │                              outreach.py, conversation_manager.py, spreadsheet_sync.py,
│   │                              notifier.py, container.py (wiring)
│   ├── repositories/              database.py (schema), lead_repository.py
│   └── models/                    lead.py, message.py, product.py, summary.py
├── config/                        business_profile.example.yaml, business_profile.demo.yaml (fictional),
│                                  qualification_rules.example.yaml
├── data/                          leads.example.csv, knowledge_base/ (SAMPLE price list + guide)
├── scripts/                       ingest_catalogue.py, demo_end_to_end.py, simulate_inbound.py,
│                                  backup_db.py, retry_dead_jobs.py, delete_lead_data.py
├── tests/                         unit + offline integration tests; tests/integration = live (opt-in)
├── .env.example  Dockerfile  docker-compose.yml  requirements*.txt  pyproject.toml
```

## 2. Quick start (mock mode – nothing is ever sent)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env                                  # leave credentials empty => mock mode
cp config/business_profile.example.yaml config/business_profile.yaml   # then fill CONFIGURE_ME fields
cp data/leads.example.csv data/leads.csv

python scripts/demo_end_to_end.py                     # full offline walkthrough, prints summary + CSV row
pytest                                                # test suite (mocks only)
uvicorn app.main:app --port 8000                      # API + CSV monitor + worker in one process
```

`WHATSAPP_MODE=auto` and `LLM_MODE=auto` switch to live **only** when credentials are present.
Set them to `mock` to force mock mode. Outside `APP_ENV=production`, set
`WHATSAPP_TEST_ALLOWLIST` so live sends can only reach your own test numbers.

In mock mode, "sent" messages are recorded in the `outbox_mock` table and logged. To play the
customer against a running server:

```bash
python scripts/simulate_inbound.py --phone +919876543210 --text "Price of WC-400?"
# or (admin key required):  POST /admin/dev/simulate-inbound {"from_phone": "...", "text": "..."}
```

## 3. Configuration

* **`.env`** – every setting is documented in `.env.example`.
* **`config/business_profile.yaml`** – company facts, contacts, tone, template preview, opt-out text.
  Any value containing `CONFIGURE_ME` is treated as *unknown*: the agent will not state it.
  `/health/ready` reports how many fields are still unconfigured.
* **`config/qualification_rules.yaml`** – weights, thresholds and review triggers.

### Input CSV
Required columns: `customer_name, company_name, mobile_number, requirement`; recommended
`lead_id`, `consent_status`; optional `email, city, state, industry, preferred_language, source`.
Header variations such as `Mobile Number`, `Phone`, `Company` are normalised.

### Lead detection & deduplication
* A row is identified by `lead_id`. Rows without one get a deterministic `AUTO-xxxxxxxxxx` id
  (hash of phone + name + company + requirement + occurrence #) which is **written back** into the CSV.
* Only *input* columns are hashed, so the agent's own write-back never looks like a new or edited row.
* First start: rows already present are recorded as `SKIPPED_EXISTING` (not contacted) unless
  `CSV_PROCESS_EXISTING_ON_STARTUP=true`. Seen ids are never re-triggered, across restarts.
* Edited input rows are updates (audited); re-processing needs `CSV_REPROCESS_EDITED_ROWS=true`
  or the operator endpoint `POST /admin/leads/{id}/retry-contact`.
* Same phone + same requirement within `DUPLICATE_WINDOW_DAYS` → `DUPLICATE_REVIEW`
  (policy `review`), contacted-but-flagged (`flag`) or ignored (`off`). Nothing is silently dropped.
* Robust reading: BOM/encodings, duplicate headers, repeated header lines, blank/short/long rows,
  half-written files (settle time + incomplete last line), locked files, editor temp-file + rename saves.
* Safe writing: lock file → **re-read** → modify only the target row → temp file + fsync →
  atomic `os.replace`. A file containing malformed lines or duplicate columns is never rewritten
  (the sync fails visibly and retries after you fix it).

## 4. Connect the Meta WhatsApp Business account

1. **Meta Business & app** – In [Meta for Developers](https://developers.facebook.com/) create an
   app of type *Business* and add the **WhatsApp** product. Link (or create) your WhatsApp
   Business Account (WABA) and register the business phone number. Complete business verification
   for production messaging limits.
2. **IDs** – From *WhatsApp › API Setup* copy the **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`
   and **WhatsApp Business Account ID** → `WHATSAPP_BUSINESS_ACCOUNT_ID`. Put the number itself in
   `WHATSAPP_COMPANY_PHONE`.
3. **Permanent token** – Business Settings › Users › *System users* › add a system user (admin),
   assign the app and the WABA, *Generate token* with `whatsapp_business_messaging` and
   `whatsapp_business_management` → `WHATSAPP_ACCESS_TOKEN`. (The 24-hour token on the API Setup
   page is only for quick tests.)
4. **App secret** – App › Settings › Basic › *App secret* → `WHATSAPP_APP_SECRET` (used to verify
   `X-Hub-Signature-256` on every webhook).
5. **Message template** – WhatsApp Manager › *Message templates* › create e.g. `lead_introduction`
   (language `en`). Body example:
   `Hello {{1}}, this is <Your Company>. Thank you for your enquiry about {{2}}. I can help with product details and pricing. Could you tell me a little about your application?`
   Set `WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANGUAGE`, and `WHATSAPP_TEMPLATE_PARAMS`
   (`customer_name,requirement` for the body above, in `{{1}}`,`{{2}}` order). Mirror the approved
   text in `outreach.template_preview` of the business profile (that copy is what the agent sees
   as conversation history). Meta decides the template category; promotional wording is
   usually *Marketing*. Wait for **Approved** status.
6. **Webhook** – Choose a long random `WHATSAPP_VERIFY_TOKEN`. In *WhatsApp › Configuration*:
   Callback URL `https://<your-domain>/webhook/whatsapp`, Verify token = the same value,
   **Verify and save**, then subscribe the **messages** field (it carries both customer messages
   and delivery statuses).
7. **Development mode** – While the app is unpublished, only numbers added under *API Setup ›
   To* can receive messages; also list them in `WHATSAPP_TEST_ALLOWLIST`.
8. Start the service and check `GET /health/ready` shows `"whatsapp_mode": "live"`.
   Optional smoke test: `RUN_LIVE_TESTS=1 LIVE_TEST_RECIPIENT=+91... pytest -m live tests/integration`.

**Consent & policy.** Only rows with an accepted `consent_status` are contacted
(`CONSENT_REQUIRED=true`). Business-initiated messages always use the approved template; free-form
replies are only sent inside the 24-hour customer-service window (error 131047 is never bypassed);
opt-outs ("stop", "don't contact me") are honoured immediately and permanently via a
do-not-contact list.

### Exposing the webhook over HTTPS
* **Local development:** `cloudflared tunnel --url http://localhost:8000` (or
  `ngrok http 8000`) and use the printed `https://…/webhook/whatsapp` as the callback URL.
  Tunnel URLs change on restart – update the Meta callback each time (or use a named tunnel).
* **Production:** run the container behind a TLS-terminating reverse proxy (Caddy, nginx,
  a cloud load balancer) on a stable domain with a valid certificate, e.g. Caddy:
  `agent.example.com { reverse_proxy localhost:8000 }`. Expose only `/webhook/whatsapp` and
  `/health` publicly; restrict `/admin/*` to your office/VPN at the proxy.

## 5. Claude API
Create a key in the Anthropic Console → `ANTHROPIC_API_KEY`; choose the model with
`ANTHROPIC_MODEL` (default `claude-sonnet-5-5`). The agent makes one structured (forced tool)
call per reply and one per summary. If Claude is unavailable after retries, the customer gets a
polite holding message and the conversation is handed to a human; summaries fall back to a
rule-based version.

## 6. Catalogue & price list

Put files in `data/knowledge_base/` (sub-folders allowed): **CSV, XLSX, PDF, DOCX, TXT/MD**
(PNG/JPG with OCR). The bundled `sample_*` files are illustrative data only – delete them.

```bash
python scripts/ingest_catalogue.py --list      # parse changed files and print what was understood
python scripts/ingest_catalogue.py --force     # re-parse everything
```
The service also re-indexes changed/removed files every `KB_REFRESH_INTERVAL_SECONDS`
(and `POST /admin/knowledge-base/reindex`). Deleted files' prices disappear immediately.

**Price-list tables** (sheets, CSVs, DOCX/PDF tables) are recognised by a model or product-id
column plus a price or name column. Recognised headers include `model / model no`, `product`,
`category`, `configuration / variant`, `price / list price / rate / price (INR)`, `currency`,
`gst / tax included` (`Extra`, `Inclusive`…), `gst rate`, `installation`, `freight`, `warranty`,
`availability`, `moq`, `valid until`, `remarks`. Everything else becomes searchable text.

**Pricing guarantees** (enforced in code, not only in the prompt):
* a price is quoted only when `PriceLookup.check` returns **VERIFIED**: exact model match
  (`WC-400` ≠ `WC-400S`), configuration identified when several exist, a numeric price with an
  explicit currency, no conflicting entries, not past `valid until` (or, without a date, the file
  is younger than `PRICE_MAX_AGE_DAYS`);
* GST is stated only as the source says; otherwise "not specified";
* conflicting entries → no price, human review; discount requests → handover;
* every outgoing reply is scanned: any amount that is not a verified price (or the customer's own
  number) replaces the reply with a safe fallback and is audited;
* each quote is written to `quotes_audit` with source file, row reference and validity date.
* OCR'd text is used for explanations only, never for quotable prices.

## 7. Google Sheets (optional)

1. Google Cloud console → enable **Google Sheets API** → create a **service account** → create a
   JSON key → save as `secrets/google-service-account.json`.
2. Share the spreadsheet with the service-account email as **Editor** (only that sheet – least
   privilege).
3. `.env`: `OUTPUT_SINKS=csv,sheets` (or `sheets`), `GOOGLE_SHEETS_SPREADSHEET_ID` (from the URL),
   `GOOGLE_SHEETS_WORKSHEET`, `GOOGLE_SERVICE_ACCOUNT_FILE=secrets/google-service-account.json`.

Rows are located by `lead_id` and updated in place; missing output columns are added to the header.
Each destination has its own sync state and retries; SQLite always keeps the result.
To use a sheet as the **lead source**, set `LEAD_SOURCE=google_sheets` (polled every
`GOOGLE_SHEETS_POLL_INTERVAL_SECONDS`); give every row a `lead_id`.

## 8. Workflow, closure and human takeover

States: `NEW_LEAD → VALIDATING → READY_TO_CONTACT → MESSAGE_SENT → (DELIVERED) →
CONVERSATION_ACTIVE ⇄ QUALIFYING / AWAITING_CUSTOMER → SUMMARY_PENDING → SHEET_SYNC_PENDING → CLOSED`,
plus `INVALID_LEAD, CONSENT_REQUIRED, DUPLICATE_REVIEW, SKIPPED_EXISTING, MESSAGE_FAILED,
HUMAN_HANDOVER, DO_NOT_CONTACT, ERROR`. Transitions are validated (`services/workflow.py`) and
audited. All work runs through a SQLite job queue with dedupe keys, per-conversation
serialisation, exponential backoff and a dead-letter state (`scripts/retry_dead_jobs.py`).

* **Closure:** customer finished/declined, opt-out, `INACTIVITY_CLOSE_MINUTES` without activity
  (only after the customer has replied), `NO_RESPONSE_CLOSE_HOURS` for leads that never replied,
  operator close, or handover. Each closure event creates exactly one summary; a later customer
  message reopens the conversation (`REOPEN_ON_INBOUND`).
* **Handover** (asked for a person, discount/negotiation, complaint, conflicting prices, AI
  unavailable): automation pauses, lead → `HUMAN_HANDOVER`, a summary is recorded, the salesperson
  is notified (log + `NOTIFY_WEBHOOK_URL`). The pause flag is checked in the same transaction that
  claims a bot reply, so no automated reply can be sent after a takeover.
* **Operator API** (`X-Admin-Key: $ADMIN_API_KEY`):
  `GET /admin/leads`, `GET /admin/leads/{id}`,
  `POST /admin/conversations/{id}/takeover|resume|close|send`,
  `POST /admin/leads/{id}/retry-contact[?approve_duplicate=true]`,
  `GET /admin/jobs`, `POST /admin/jobs/retry-dead`, `POST /admin/knowledge-base/reindex`.

## 9. Database & backups

The schema is created automatically on start (`DATABASE_PATH`, WAL mode).
`python scripts/backup_db.py --dest backups --keep 14` makes a consistent online backup – schedule
it (cron / systemd timer). Restore = stop the service and copy a backup over `agent.db`.
`RETENTION_DAYS` purges old messages of closed conversations; `scripts/delete_lead_data.py`
erases one lead's conversation data on request.

## 10. Docker

```bash
cp .env.example .env   # fill in
mkdir -p secrets backups && cp data/leads.example.csv data/leads.csv
docker compose up -d --build
docker compose logs -f agent
```
Run **one** replica (SQLite + file watching are single-node). `data/` holds the CSV, knowledge
base and database; `config/` is mounted read-only.

## 11. Tests

```bash
pytest                                       # offline: mock WhatsApp + mock Claude
RUN_LIVE_TESTS=1 ANTHROPIC_API_KEY=... pytest -m live tests/integration     # real Claude call
```
Covered: new-row trigger, restart idempotency, output-column edits, generated ids, duplicates,
messy CSVs, atomic-rename saves, write-back preservation, phone/consent/DNC validation, webhook
signature/handshake/duplicates/statuses/media/unknown senders, lead matching, context across
restarts, exact/unknown/ambiguous/conflicting/expired prices, tax not assumed, hallucinated-price
blocking, discount escalation, opt-out, factual summary (exactly once), inactivity close & reopen,
handover races, Sheets retries without duplicate rows, permanent/transient/window-expired send
failures, crash recovery, dead-letter retry, prompt injection (customer and catalogue), XLSX/DOCX/
PDF parsing and scanned-PDF detection, Cloud API request format. `tests/test_api.py` exercises the
FastAPI layer when FastAPI is installed.

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| Meta "callback URL could not be validated" | URL is public HTTPS, path `/webhook/whatsapp`, `WHATSAPP_VERIFY_TOKEN` matches exactly, service running |
| Webhooks return 401 | `WHATSAPP_APP_SECRET` is the secret of the **same** Meta app that sends the webhook |
| No inbound events | `messages` field subscribed; app linked to the WABA; number not in another app |
| Intro fails with 132001 / 132000 | Template name/language mismatch or param count ≠ `WHATSAPP_TEMPLATE_PARAMS` |
| 131026 undeliverable | Not a WhatsApp number, or recipient not allowed in development mode |
| 131047 re-engagement | > 24h since the customer's last message; a human must send a template |
| 131056 / 130429 | Rate/pair limits – jobs retry with backoff; lower `WHATSAPP_SEND_RATE_PER_SECOND` |
| Lead stuck in `CONSENT_REQUIRED` | `consent_status` not in `CONSENT_ACCEPTED_VALUES`; fix row, then `retry-contact` |
| CSV not updated, `sync_status` FAILED | File locked by Excel, malformed lines or duplicate columns; fix/close the file, `retry_dead_jobs.py --retry` |
| Existing rows not contacted | Expected on first start; set `CSV_PROCESS_EXISTING_ON_STARTUP=true` before the first start |
| Agent never quotes a price | `ingest_catalogue.py --list` – currency, validity date, duplicates (CONFLICT), configuration |
| "database is locked" | Only one service instance per database file |

## 13. Known limitations

* Automatic follow-up nudges outside the 24-hour window are not sent (they need approved
  templates and a business decision); the summary recommends the next action instead.
* Media messages are acknowledged by type/caption; images/documents sent by customers are not
  downloaded or analysed.
* Unknown senders (not in any lead row) are stored and notified, not auto-answered.
* Language: replies follow the customer's language when Claude is used; the mock agent is English.
* Single-node design (SQLite); for multiple replicas move the queue/state to PostgreSQL.
* Delivery is at-least-once: if the process dies exactly between WhatsApp accepting a message and
  the database recording it, that one message may be sent twice on recovery.
