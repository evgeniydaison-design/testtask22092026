"""run-explainer HTML, provenance fingerprint and DSAR subject export."""

from __future__ import annotations

from lead_engine import audit_chain, calibration, fuzz, provenance
from lead_engine.approval import human_gate
from lead_engine.audit import subject_export
from lead_engine.metrics import funnel as funnel_mod
from lead_engine.run_report import render_run_explainer, state_machine_svg

from .conftest import ALPHA, BETA, OPTOUT, STRONG


def test_state_machine_svg_is_data_driven():
    svg = state_machine_svg()
    assert svg.startswith("<div") and "<svg" in svg and "</svg>" in svg
    for stage in ("received", "deduped", "manual_review", "human_approved", "outboxed", "dlq"):
        assert stage in svg
    # the human-gated edge is drawn in the red "gated" colour
    assert "#c54b4b" in svg


def test_provenance_shape(tmp_path, settings):
    prov = provenance.collect(settings, root=tmp_path)
    assert set(prov) >= {"git_commit", "python", "platform", "fixtures_digest", "config", "fingerprint"}
    assert len(prov["fingerprint"]) == 16
    assert "ai_confidence_threshold" in prov["config"]


def _assemble(make):
    svc = make(ALPHA)
    for raw in (STRONG, OPTOUT):
        r = svc.pipeline.ingest_raw(raw, "csv")
        svc.pipeline.qualify_lead(r.lead_id)
    human_gate.approve_lead(svc.repo, svc.repo.list_leads()[0]["id"], actor="ops-alpha")
    audit_chain.seal(svc.repo, svc.settings.audit_seal_path(ALPHA))
    metrics = [funnel_mod.compute(svc.repo, svc.crm, svc.outbox)]
    calibs = [calibration.compute(svc.pipeline)]
    chains = [audit_chain.verify(svc.repo, svc.settings.audit_seal_path(ALPHA))]
    fz = fuzz.run_fuzz(svc.settings.data_dir / "fz", n=60, seed=5).as_dict()
    prov = provenance.collect(svc.settings)
    return svc, metrics, calibs, chains, fz, prov


def test_render_run_explainer_sections(make):
    svc, metrics, calibs, chains, fz, prov = _assemble(make)
    html = render_run_explainer(metrics, calibs, chains, fz, prov)
    assert "<!doctype html>" in html.lower()
    for marker in ("Stage machine", "calibration", "Adversarial fuzz", "audit chain", "Provenance"):
        assert marker in html
    assert ALPHA in html
    assert "#c54b4b" in html  # svg present
    assert chains[0]["status"] == "verified"


def test_subject_export_found(make):
    svc = make(ALPHA)
    r = svc.pipeline.ingest_raw(STRONG, "csv")
    svc.pipeline.qualify_lead(r.lead_id)
    out = subject_export(svc.repo, svc.crm, svc.outbox, "Ava.Stone.X1@example.com")
    assert out["found"] is True
    assert out["normalized_email"] == "ava.stone.x1@example.com"
    assert out["record_count"] >= 1
    assert out["records"][0]["lead_id"] == r.lead_id


def test_subject_export_unknown_and_cross_tenant(make):
    svc = make(ALPHA)
    svc.pipeline.ingest_raw(STRONG, "csv")
    # unknown subject
    assert subject_export(svc.repo, svc.crm, svc.outbox, "nobody@nowhere.example")["found"] is False
    # ALPHA lead is invisible to BETA (physical isolation)
    beta = make(BETA)
    assert subject_export(beta.repo, beta.crm, beta.outbox, "ava.stone.x1@example.com")["found"] is False
