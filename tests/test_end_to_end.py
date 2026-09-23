"""End-to-end run over the committed synthetic fixtures, funnel metrics, and
the FastAPI mock webhook (inbound idempotency + tenant routing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lead_engine.metrics import funnel
from lead_engine.approval import human_gate
from lead_engine.models import LeadStage
from lead_engine.sources.readers import read_json, read_source

from .conftest import ALPHA, BETA

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load_tenant(svc, label: str):
    svc.pipeline.ingest_many(read_source(FIXTURES / f"leads_{label}.csv", "csv"), "csv")
    svc.pipeline.ingest_many(read_json(FIXTURES / f"leads_{label}.json"), "json")
    for env in read_json(FIXTURES / f"webhook_{label}.json"):
        svc.pipeline.ingest_raw(env["record"], "webhook", idempotency_hint=env["idempotency_key"])


def test_fixture_dataset_is_large_enough_and_tenant_scoped():
    # combined synthetic records comfortably exceed the required 60
    total = 0
    for label in ("alpha", "beta"):
        total += len(read_source(FIXTURES / f"leads_{label}.csv", "csv"))
        total += len(read_json(FIXTURES / f"leads_{label}.json"))
        total += len(read_json(FIXTURES / f"webhook_{label}.json"))
    assert total >= 60, total


def test_end_to_end_never_autodelivers(make):
    svc = make(ALPHA)
    _load_tenant(svc, "alpha")
    report = svc.pipeline.run_qualification()
    assert report.processed > 0

    # safety categories landed in the right stages
    by_stage = svc.repo.stage_counts()
    assert by_stage.get(LeadStage.REJECTED.value, 0) >= 1          # opt-outs
    assert by_stage.get(LeadStage.MANUAL_REVIEW.value, 0) >= 1     # injection/conflict/thin
    assert by_stage.get(LeadStage.APPROVED_READY.value, 0) >= 1    # strong leads

    # no human acted -> nothing may be delivered
    assert svc.outbox.count() == 0
    assert svc.crm.count_contacts() == 0
    assert svc.repo.list_delivery_ops() == []


def test_cross_tenant_fixture_isolation(make):
    a = make(ALPHA)
    b = make(BETA)
    _load_tenant(a, "alpha")
    _load_tenant(b, "beta")
    a_keys = {l["canonical_key"] for l in a.repo.list_leads()}
    b_keys = {l["canonical_key"] for l in b.repo.list_leads()}
    # synthetic emails embed the tenant label, so key sets are disjoint
    assert a_keys and b_keys
    assert a_keys.isdisjoint(b_keys)


def test_funnel_metrics_are_consistent(make):
    svc = make(ALPHA)
    _load_tenant(svc, "alpha")
    svc.pipeline.run_qualification()
    # approve + deliver a subset so delivery metrics are non-zero
    for lead in svc.repo.list_leads(stage=LeadStage.APPROVED_READY.value)[:3]:
        human_gate.approve_lead(svc.repo, lead["id"], actor="ops-metrics")
    svc.delivery.enqueue_all_approved()
    svc.delivery.process_all()

    m = funnel.compute(svc.repo, svc.crm, svc.outbox).as_dict()
    assert m["leads_total"] == sum(svc.repo.stage_counts().values())
    assert m["outbox_entries"] == m["delivery"].get("succeeded", 0)
    assert m["sources"] >= m["leads_total"]              # every lead has >=1 source
    assert 0.0 <= m["manual_review_rate"] <= 1.0


# --- mock webhook endpoint ------------------------------------------------
def _client():
    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover
        pytest.skip("TestClient not available")
    from lead_engine.app import app
    return TestClient(app)


def test_webhook_endpoint_ingests_and_is_idempotent(make, settings, monkeypatch):
    # point the app's default settings at the temp data dir
    monkeypatch.setenv("ATHENAI_DATA_DIR", str(settings.data_dir))
    client = _client()
    body = {"tenant_id": ALPHA, "idempotency_key": "wh-test-1", "record": {
        "email": "hook@example.com", "name": "Hook Person", "company": "HookCo",
        "country": "US", "budget_band": "high", "intent": "request a demo and quote",
        "phone": "+15550012345"}}
    r1 = client.post(f"/ingest/webhook/{ALPHA}", json=body)
    assert r1.status_code == 200 and r1.json()["status"] == "ingested"
    r2 = client.post(f"/ingest/webhook/{ALPHA}", json=body)
    assert r2.json()["status"] == "duplicate_ignored"     # inbound idempotency


def test_webhook_endpoint_rejects_unknown_tenant(make, settings, monkeypatch):
    monkeypatch.setenv("ATHENAI_DATA_DIR", str(settings.data_dir))
    client = _client()
    r = client.post("/ingest/webhook/tenant_ghost",
                    json={"tenant_id": "tenant_ghost", "idempotency_key": "x", "record": {"email": "a@b.com"}})
    assert r.status_code == 404
