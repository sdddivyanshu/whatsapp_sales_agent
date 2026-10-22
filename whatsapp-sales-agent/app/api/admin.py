"""Operator API (requires ``X-Admin-Key``; disabled when ADMIN_API_KEY is empty).

Lets authorised sales staff inspect leads, take over / resume / close
conversations, reply as a human, retry failed work and re-index the catalogue.
"""

from __future__ import annotations

import hmac
from typing import Any

from app.repositories.database import loads


def lead_detail(container: Any, lead_id: str) -> dict[str, Any] | None:
    lead = container.db.query_one("SELECT * FROM leads WHERE lead_id=?", (lead_id,))
    if lead is None:
        return None
    convs = container.db.query("SELECT * FROM conversations WHERE lead_id=? ORDER BY conversation_id",
                               (lead_id,))
    out = {k: lead[k] for k in lead.keys() if k not in {"raw_json"}}
    out["conversations"] = []
    for c in convs:
        msgs = container.db.query("SELECT id, direction, sender, msg_type, body, status, created_at "
                                  "FROM messages WHERE conversation_id=? ORDER BY id", (c["conversation_id"],))
        out["conversations"].append({
            **{k: c[k] for k in c.keys() if k != "memory_json"},
            "memory": loads(c["memory_json"], {}),
            "messages": [dict(m) for m in msgs],
        })
    out["summaries"] = [{"closure_seq": s["closure_seq"], "summary": loads(s["summary_json"], {}),
                         "created_at": s["created_at"]} for s in container.db.query(
        "SELECT * FROM summaries WHERE lead_id=? ORDER BY id", (lead_id,))]
    out["sync"] = [dict(r) for r in container.db.query("SELECT * FROM sync_status WHERE lead_id=?", (lead_id,))]
    return out


def build_router(container_getter):  # pragma: no cover - requires FastAPI at runtime
    from fastapi import APIRouter, Depends, Header, HTTPException
    from pydantic import BaseModel

    router = APIRouter(prefix="/admin")

    def require_admin(x_admin_key: str = Header(default="")) -> None:
        expected = container_getter().settings.admin_api_key
        if not expected:
            raise HTTPException(403, "admin API disabled: set ADMIN_API_KEY")
        if not hmac.compare_digest(x_admin_key, expected):
            raise HTTPException(401, "invalid admin key")

    class OperatorBody(BaseModel):
        operator: str
        reason: str | None = None

    class SendBody(BaseModel):
        operator: str
        text: str

    class SimulateBody(BaseModel):
        from_phone: str
        text: str

    deps = [Depends(require_admin)]

    @router.get("/leads", dependencies=deps)
    def list_leads(status: str | None = None, limit: int = 100) -> list[dict]:
        return [dict(r) for r in container_getter().outreach.leads.list(status, limit)]

    @router.get("/leads/{lead_id}", dependencies=deps)
    def get_lead(lead_id: str) -> dict:
        data = lead_detail(container_getter(), lead_id)
        if data is None:
            raise HTTPException(404, "lead not found")
        return data

    @router.post("/leads/{lead_id}/retry-contact", dependencies=deps)
    def retry_contact(lead_id: str, approve_duplicate: bool = False) -> dict:
        try:
            container_getter().outreach.retry_contact(lead_id, approve_duplicate)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "queued"}

    @router.post("/conversations/{conv_id}/takeover", dependencies=deps)
    def takeover(conv_id: int, body: OperatorBody) -> dict:
        container_getter().conversations.take_over(conv_id, body.operator, body.reason or "Operator took over")
        return {"status": "human"}

    @router.post("/conversations/{conv_id}/resume", dependencies=deps)
    def resume(conv_id: int, body: OperatorBody) -> dict:
        container_getter().conversations.resume_automation(conv_id, body.operator)
        return {"status": "automated"}

    @router.post("/conversations/{conv_id}/close", dependencies=deps)
    def close(conv_id: int, body: OperatorBody) -> dict:
        container_getter().conversations.request_close(conv_id, "operator_closed")
        return {"status": "closing"}

    @router.post("/conversations/{conv_id}/send", dependencies=deps)
    def send(conv_id: int, body: SendBody) -> dict:
        try:
            wamid = container_getter().conversations.human_send(conv_id, body.operator, body.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "sent", "wa_message_id": wamid}

    @router.post("/jobs/retry-dead", dependencies=deps)
    def retry_dead(job_id: int | None = None) -> dict:
        return {"requeued": container_getter().queue.retry_dead(job_id)}

    @router.get("/jobs", dependencies=deps)
    def jobs(status: str = "DEAD", limit: int = 50) -> list[dict]:
        return [dict(r) for r in container_getter().db.query(
            "SELECT id, job_type, status, attempts, last_error, payload_json, updated_at FROM jobs "
            "WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))]

    @router.post("/knowledge-base/reindex", dependencies=deps)
    def reindex(force: bool = False) -> dict:
        return container_getter().refresh_knowledge_base(force=force).as_dict()

    @router.post("/dev/simulate-inbound", dependencies=deps)
    def simulate_inbound(body: SimulateBody) -> dict:
        """Mock mode only: inject a customer message as if it came from the webhook."""
        container = container_getter()
        if container.settings.whatsapp_live:
            raise HTTPException(403, "only available in mock mode")
        import uuid

        from app.models.message import InboundMessage, ParsedWebhook

        msg = InboundMessage(wa_message_id=f"wamid.SIM{uuid.uuid4().hex[:16]}", from_phone=body.from_phone,
                             msg_type="text", text=body.text)
        return container.conversations.handle_webhook(ParsedWebhook(messages=[msg]))

    return router
