"""Operator web UI tests (fastapi.testclient.TestClient).

The point of this suite is to prove the UI is a *thin* layer: it must reuse the
exact same approval gate, tenant isolation, and draft-quarantine invariants the
CLI already relies on - never introduce a second decision path. Each request
opens a tenant-scoped repository, so isolation is structural, not a filter.

Isolation wiring: the routes call ``build_service(tenant)`` with no explicit
settings, so ``load_settings()`` reads ``ATHENAI_DATA_DIR`` at request time. We
point that env var at the same temp ``data`` dir the ``make`` fixture uses, so a
lead seeded via the fixture and the page served by TestClient hit one store.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lead_engine.app import app
from lead_engine.approval import human_gate
from lead_engine.models import LeadStage

from .conftest import ALPHA, BETA, INJECTION, OPTOUT, STRONG, THIN


@pytest.fixture
def client(tmp_path, monkeypatch, make):
    monkeypatch.setenv("ATHENAI_DATA_DIR", str(tmp_path / "data"))
    return TestClient(app)


def _qualify(svc, raw):
    """Ingest + qualify one raw record through the real pipeline; return lead_id."""
    res = svc.pipeline.ingest_raw(raw, "csv")
    svc.pipeline.qualify_lead(res.lead_id)
    return res.lead_id


def _stage(svc, lead_id: str) -> str:
    return svc.repo.get_lead(lead_id)["stage"]


def test_queue_is_tenant_isolated(client, make):
    """tenant_alpha's queue never surfaces tenant_beta's lead (and vice versa)."""
    alpha_lead = _qualify(make(ALPHA), THIN)      # manual_review -> pending queue
    beta_lead = _qualify(make(BETA), STRONG)      # approved_ready -> pending queue

    alpha_html = client.get(f"/ui/{ALPHA}/queue").text
    assert alpha_lead in alpha_html
    assert beta_lead not in alpha_html

    beta_html = client.get(f"/ui/{BETA}/queue").text
    assert beta_lead in beta_html
    assert alpha_lead not in beta_html


def test_approve_via_ui_equals_cli(client, make):
    """Approving through the UI calls the same gate the CLI calls: the lead moves
    to human_approved AND a real approve decision row (with the actor) is written."""
    lead_id = _qualify(make(ALPHA), THIN)         # manual_review

    r = client.post(
        f"/ui/{ALPHA}/lead/{lead_id}/approve",
        data={"actor": "ops-alpha", "note": "approved via web"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/{ALPHA}/queue"

    svc = make(ALPHA)
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.HUMAN_APPROVED.value
    approvals = [d for d in svc.repo.list_decisions(lead_id) if d["action"] == "approve"]
    assert len(approvals) == 1
    assert approvals[0]["actor"] == "ops-alpha"


def test_approve_without_actor_is_rejected_and_leaves_stage(client, make):
    """No actor -> HTTP 400, the same ApprovalError the CLI hits without --actor,
    and the lead is left untouched (no decision, no partial write)."""
    lead_id = _qualify(make(ALPHA), THIN)

    for payload in ({"note": "no actor here"}, {"actor": "   ", "note": "blank"}):
        r = client.post(f"/ui/{ALPHA}/lead/{lead_id}/approve", data=payload)
        assert r.status_code == 400
        assert "actor is required" in r.text

    svc = make(ALPHA)
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.MANUAL_REVIEW.value
    assert svc.repo.list_decisions(lead_id) == []


def test_reject_via_ui_blocks_delivery(client, make):
    """Reject mirrors approve: moves to human_rejected and the gate still refuses
    any later delivery attempt."""
    lead_id = _qualify(make(ALPHA), THIN)

    r = client.post(
        f"/ui/{ALPHA}/lead/{lead_id}/reject",
        data={"actor": "ops-alpha", "note": "not qualified"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    svc = make(ALPHA)
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.HUMAN_REJECTED.value
    assert any(d["action"] == "reject" for d in svc.repo.list_decisions(lead_id))
    with pytest.raises(human_gate.ApprovalError):
        svc.delivery.enqueue(lead_id)


def test_injection_lead_detail_shows_quarantine_not_payload(client, make):
    """The reusable draft invariant surfaced through the UI: for a prompt-injection
    lead the *draft* (the only text that can ever reach an outbound message) is the
    neutral quarantine stub, not an echo of the malicious free-text. The raw-source
    panel still shows the original payload on purpose - that is the audit trail -
    but every value is HTML-escaped, so it renders as inert text, never live markup.
    """
    lead_id = _qualify(make(ALPHA), INJECTION)    # injection -> manual_review + quarantined
    assert _stage(make(ALPHA), lead_id) == LeadStage.MANUAL_REVIEW.value

    # stored draft (what delivery would send) is the stub, not the payload
    draft_body = make(ALPHA).repo.get_draft_for_lead(lead_id)["body"]
    assert draft_body.startswith("[Quarantined draft]")
    assert "Ignore all previous instructions" not in draft_body

    page = client.get(f"/ui/{ALPHA}/lead/{lead_id}").text
    assert "Quarantined draft" in page            # operator sees the neutral stub
    assert "<script" not in page.lower()          # nothing rendered as live markup


def test_double_approve_is_idempotent(client, make):
    """A second approve is refused by the state machine (409) and never writes a
    second decision, so the delivery path stays single-shot."""
    lead_id = _qualify(make(ALPHA), THIN)

    r1 = client.post(
        f"/ui/{ALPHA}/lead/{lead_id}/approve",
        data={"actor": "ops-alpha", "note": "first"}, follow_redirects=False,
    )
    assert r1.status_code == 303

    r2 = client.post(
        f"/ui/{ALPHA}/lead/{lead_id}/approve",
        data={"actor": "ops-alpha", "note": "second"}, follow_redirects=False,
    )
    assert r2.status_code == 409
    assert "already approved" in r2.text

    svc = make(ALPHA)
    decisions = svc.repo.list_decisions(lead_id)
    assert len(decisions) == 1
    assert decisions[0]["action"] == "approve"
    # gate still considers it deliverable exactly once (no duplicate approval row)
    assert sum(1 for d in decisions if d["action"] == "approve") == 1


def test_optout_lead_cannot_be_approved_via_ui(client, make):
    """Defense-in-depth reused from the gate: an already-rejected opt-out lead is
    not approvable from the UI either (stage is terminal -> 409)."""
    lead_id = _qualify(make(ALPHA), OPTOUT)       # opt-out -> rejected (terminal)
    assert _stage(make(ALPHA), lead_id) == LeadStage.REJECTED.value

    r = client.post(
        f"/ui/{ALPHA}/lead/{lead_id}/approve",
        data={"actor": "ops-alpha", "note": "try"}, follow_redirects=False,
    )
    assert r.status_code == 409
    assert _stage(make(ALPHA), lead_id) == LeadStage.REJECTED.value


def test_cross_tenant_lead_id_not_found(client, make):
    """A lead id from tenant_beta is 404 when addressed under tenant_alpha - the
    tenant-scoped repo physically cannot see another tenant's lead."""
    beta_lead = _qualify(make(BETA), THIN)
    r = client.get(f"/ui/{ALPHA}/lead/{beta_lead}")
    assert r.status_code == 404


def test_metrics_page_reuses_dashboard(client, make):
    """The metrics endpoint reuses the CLI report dashboard generator."""
    _qualify(make(ALPHA), STRONG)
    r = client.get(f"/ui/{ALPHA}/metrics")
    assert r.status_code == 200
    assert "funnel report" in r.text
    assert ALPHA in r.text
