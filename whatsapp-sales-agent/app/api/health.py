"""Liveness / readiness information."""

from __future__ import annotations

from typing import Any

from app.services.container import AppContainer


def health_status(container: AppContainer) -> dict[str, Any]:
    db_ok = True
    try:
        container.db.query_one("SELECT 1")
    except Exception:  # noqa: BLE001
        db_ok = False
    settings = container.settings
    kb = container.products.sources()
    problems = settings.validate()
    return {
        "status": "ok" if db_ok and not problems else "degraded",
        "database": db_ok,
        "whatsapp_mode": "live" if settings.whatsapp_live else "mock",
        "llm_mode": "live" if settings.llm_live else "mock",
        "lead_source": settings.lead_source,
        "output_sinks": settings.output_sinks,
        "csv_monitor_error": container.csv_monitor.last_error,
        "jobs": container.queue.stats(),
        "knowledge_base": {
            "files": len(kb),
            "products": sum(s["product_count"] for s in kb),
            "files_with_warnings": [s["source_file"] for s in kb if s["status"] != "OK"],
        },
        "config_problems": problems,
        "unconfigured_profile_fields": len(container.profile.unconfigured_fields()),
    }


def build_router(container_getter):  # pragma: no cover - requires FastAPI at runtime
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/health")
    def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @router.get("/health/ready")
    def readiness() -> JSONResponse:
        data = health_status(container_getter())
        return JSONResponse(data, status_code=200 if data["database"] else 503)

    return router
