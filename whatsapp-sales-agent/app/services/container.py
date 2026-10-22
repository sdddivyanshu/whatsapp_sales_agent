"""Composition root: builds and wires every component from ``Settings``.

Used by the FastAPI app, the scripts and the tests, so all of them run the
exact same code paths. Collaborators (messaging provider, LLM, Sheets backend,
notifier) can be injected, which is how tests run fully offline.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.mock_responses import build_mock_llm
from app.agents.qualification import QualificationEngine
from app.agents.sales_agent import SalesAgent
from app.agents.summarizer import Summarizer
from app.config import Settings, load_business_profile, load_qualification_rules
from app.ingestion.csv_monitor import JOB_CONTACT_LEAD, CsvLeadMonitor, LeadIngestor
from app.integrations.anthropic_client import AnthropicClient, LLMClient
from app.integrations.google_sheets import (
    GoogleSheetsApiBackend, GoogleSheetsLeadSource, GoogleSheetsSink, SheetsBackend,
)
from app.integrations.whatsapp_cloud import (
    MessagingProvider, MockWhatsAppProvider, WhatsAppCloudClient, WhatsAppError,
)
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.price_lookup import PriceLookup
from app.knowledge.product_repository import IngestReport, ProductRepository
from app.knowledge.retrieval import BM25Retriever
from app.models.message import SendResult
from app.repositories.database import Database
from app.services.conversation_manager import (
    JOB_CLOSE, JOB_PROCESS, JOB_SUMMARIZE, ConversationManager,
)
from app.services.message_queue import JobQueue, Worker
from app.services.notifier import Notifier
from app.services.outreach import JOB_NOTIFY, OutreachService
from app.services.spreadsheet_sync import JOB_SYNC_OUTPUT, CsvSink, SyncService

log = logging.getLogger(__name__)


class AllowlistProvider:
    """Safety wrapper: outside production, live sends only reach allow-listed numbers."""

    def __init__(self, inner: MessagingProvider, allowed: list[str]) -> None:
        self.inner = inner
        self.allowed = {"+" + "".join(ch for ch in a if ch.isdigit()) for a in allowed}

    def _check(self, to: str) -> None:
        if to not in self.allowed:
            raise WhatsAppError(f"{to[:5]}… is not in WHATSAPP_TEST_ALLOWLIST (non-production)",
                                retryable=False)

    def send_template(self, to: str, template_name: str, language: str, body_params: list[str]) -> SendResult:
        self._check(to)
        return self.inner.send_template(to, template_name, language, body_params)

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        self._check(to)
        return self.inner.send_text(to, body, reply_to)

    def mark_read(self, wa_message_id: str) -> None:
        self.inner.mark_read(wa_message_id)


class AppContainer:
    def __init__(self, settings: Settings, *, provider: MessagingProvider | None = None,
                 llm: LLMClient | None = None, sheets_backend: SheetsBackend | None = None,
                 notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.db.init()
        self.profile = load_business_profile(settings)
        self.queue = JobQueue(self.db, settings.job_max_attempts, settings.job_backoff_base_seconds,
                              settings.job_stale_lock_seconds)

        if provider is None:
            if settings.whatsapp_live:
                provider = WhatsAppCloudClient(
                    access_token=settings.whatsapp_access_token,
                    phone_number_id=settings.whatsapp_phone_number_id,
                    api_version=settings.whatsapp_graph_api_version,
                    base_url=settings.whatsapp_graph_base_url,
                    timeout=settings.whatsapp_timeout_seconds,
                    rate_per_second=settings.whatsapp_send_rate_per_second)
                if settings.app_env != "production" and settings.whatsapp_test_allowlist:
                    provider = AllowlistProvider(provider, settings.whatsapp_test_allowlist)
            else:
                provider = MockWhatsAppProvider(self.db)
        self.provider = provider

        if llm is None:
            llm = (AnthropicClient(settings.anthropic_api_key, settings.anthropic_model,
                                   settings.anthropic_max_tokens, settings.anthropic_timeout_seconds,
                                   settings.anthropic_max_retries)
                   if settings.llm_live else build_mock_llm())
        self.llm = llm

        self.products = ProductRepository(self.db, DocumentLoader(
            default_currency=self.profile.known("pricing.default_currency"),
            ocr_enabled=settings.ocr_enabled, ocr_language=settings.ocr_language))
        self.price_lookup = PriceLookup(self.products, settings.price_max_age_days)
        self.retriever = BM25Retriever(self.db)
        self.agent = SalesAgent(llm, self.profile, self.price_lookup, self.retriever,
                                secrets=settings.secret_values(),
                                company_phones=[settings.whatsapp_company_phone],
                                history_limit=settings.history_messages_for_llm)
        self.qualification = QualificationEngine(load_qualification_rules(settings))
        self.summarizer = Summarizer(llm, self.profile)
        self.fallback_summarizer = Summarizer(build_mock_llm(), self.profile)
        self.notifier = notifier or Notifier(settings.notify_webhook_url, settings.notify_timeout_seconds)

        self._sheets_backend = sheets_backend
        sheets_factory = None
        if "sheets" in settings.output_sinks:
            sheets_factory = lambda: GoogleSheetsSink(self.sheets_backend(), settings.google_sheets_key_column)  # noqa: E731
        self.sync = SyncService(
            self.db, self.queue, settings.output_sinks,
            csv_sink=CsvSink(settings.leads_csv_path) if "csv" in settings.output_sinks else None,
            sheets_sink_factory=sheets_factory)

        self.outreach = OutreachService(settings, self.db, self.queue, self.provider, self.profile, self.sync)
        self.conversations = ConversationManager(
            settings, self.db, self.queue, self.provider, self.agent, self.qualification,
            self.summarizer, self.fallback_summarizer, self.sync, self.notifier)
        self.ingestor = LeadIngestor(settings, self.db, self.queue)
        self.csv_monitor = CsvLeadMonitor(settings, self.db, self.ingestor)

        periodic: list[tuple[float, Any]] = [
            (30.0, self.conversations.check_inactivity),
            (settings.kb_refresh_interval_seconds, self.refresh_knowledge_base),
            (3600.0, self.conversations.purge_old_data),
            (60.0, self.queue.recover_stale),
        ]
        if settings.lead_source == "google_sheets":
            periodic.append((settings.google_sheets_poll_interval_seconds, self.poll_sheet_leads))
        self.worker = Worker(self.queue, self.handlers(), settings.worker_poll_interval_seconds, periodic)

    # ------------------------------------------------------------------
    def sheets_backend(self) -> SheetsBackend:
        if self._sheets_backend is None:
            self._sheets_backend = GoogleSheetsApiBackend(
                self.settings.google_sheets_spreadsheet_id, self.settings.google_sheets_worksheet,
                self.settings.google_service_account_file)
        return self._sheets_backend

    def handlers(self) -> dict[str, Any]:
        return {
            JOB_CONTACT_LEAD: self.outreach.handle_contact,
            JOB_PROCESS: self.conversations.handle_process,
            JOB_CLOSE: self.conversations.handle_close,
            JOB_SUMMARIZE: self.conversations.handle_summarize,
            JOB_SYNC_OUTPUT: lambda job: self.sync.handle_job(job, self.conversations.on_all_synced),
            JOB_NOTIFY: self.conversations.handle_notify,
        }

    def refresh_knowledge_base(self, force: bool = False) -> IngestReport:
        report = self.products.ingest_directory(self.settings.knowledge_base_dir, force=force)
        for name, warnings in report.warnings.items():
            for w in warnings:
                log.warning("Knowledge base %s: %s", name, w)
        for name, error in report.errors.items():
            log.error("Knowledge base %s failed: %s", name, error)
        return report

    def poll_sheet_leads(self) -> None:
        source = GoogleSheetsLeadSource(self.sheets_backend())
        key = f"sheet_baseline:{self.settings.google_sheets_spreadsheet_id}"
        baseline = self.db.get_meta(key) is None and not self.settings.csv_process_existing_on_startup
        self.ingestor.ingest(source.fetch_rows(), source="google_sheets", baseline=baseline)
        self.db.set_meta(key, "done")

    def start(self) -> None:
        problems = self.settings.validate()
        for p in problems:
            log.error("Configuration problem: %s", p)
        missing = self.profile.unconfigured_fields()
        if missing:
            log.warning("Business profile has %d unconfigured field(s), e.g. %s", len(missing),
                        ", ".join(missing[:5]))
        log.info("Starting: WhatsApp=%s, LLM=%s, sinks=%s, source=%s",
                 "LIVE" if self.settings.whatsapp_live else "MOCK",
                 "LIVE" if self.settings.llm_live else "MOCK",
                 ",".join(self.settings.output_sinks), self.settings.lead_source)
        self.refresh_knowledge_base()
        if self.settings.lead_source == "csv":
            self.csv_monitor.start()
        self.worker.start()

    def stop(self) -> None:
        self.csv_monitor.stop()
        self.worker.stop()
