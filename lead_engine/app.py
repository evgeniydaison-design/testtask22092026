"""FastAPI app: mock webhook ingestion + read-only operator/metrics views.

Local only. The webhook validates the tenant, enforces inbound idempotency, and
runs normalize -> dedup -> qualify. It never delivers: delivery still requires a
human decision made via the CLI/approval gate.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .metrics import funnel
from .approval import human_gate
from .config import get_tenant
from .models import WebhookPayload
from .service import build_service
from .sources.readers import record_from_webhook

app = FastAPI(title="AthenAI Lead Engine", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "synthetic_only": True}


@app.post("/ingest/webhook/{tenant_id}")
def ingest_webhook(tenant_id: str, payload: WebhookPayload) -> dict:
    try:
        get_tenant(tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown tenant")
    if payload.tenant_id and payload.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="tenant mismatch in path vs body")

    svc = build_service(tenant_id)
    try:
        raw = record_from_webhook(payload.model_dump())
        res = svc.pipeline.ingest_raw(raw, "webhook", idempotency_hint=payload.idempotency_key)
        if res is None:
            return {"status": "duplicate_ignored", "idempotency_key": payload.idempotency_key}
        svc.pipeline.qualify_lead(res.lead_id)
        lead = svc.repo.get_lead(res.lead_id)
        return {
            "status": "ingested",
            "lead_id": res.lead_id,
            "stage": lead["stage"] if lead else None,
            "is_duplicate": res.is_duplicate,
        }
    finally:
        svc.close()


@app.get("/queue/{tenant_id}")
def queue(tenant_id: str) -> list[dict]:
    try:
        get_tenant(tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown tenant")
    svc = build_service(tenant_id)
    try:
        return human_gate.pending_queue(svc.repo)
    finally:
        svc.close()


@app.get("/metrics/{tenant_id}")
def metrics(tenant_id: str) -> dict:
    try:
        get_tenant(tenant_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown tenant")
    svc = build_service(tenant_id)
    try:
        return funnel.compute(svc.repo, svc.crm, svc.outbox).as_dict()
    finally:
        svc.close()
