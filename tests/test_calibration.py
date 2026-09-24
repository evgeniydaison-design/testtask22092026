"""Calibration: disagreement matrix + monotonic threshold sweep."""

from __future__ import annotations

from lead_engine import calibration

from .conftest import INJECTION, OPTOUT, STRONG, THIN


def _load(make):
    svc = make()
    for raw in (STRONG, THIN, OPTOUT, INJECTION):
        svc.pipeline.ingest_raw(raw, "csv")
    svc.pipeline.run_qualification()
    return svc


def test_disagreement_matrix_totals(make):
    svc = _load(make)
    d = calibration.disagreement_matrix(svc.pipeline)
    qualified = [l for l in svc.repo.list_leads() if l.get("rules_verdict")]
    assert d["total"] == len(qualified)
    # sum of the 3x3 cells equals the total qualified leads
    assert sum(sum(row.values()) for row in d["matrix"].values()) == d["total"]


def test_sweep_is_monotonic(make):
    svc = _load(make)
    cal = calibration.threshold_sweep(svc.pipeline)
    assert cal["monotonic"] is True
    approvals = [r["approve_ready"] for r in sorted(cal["sweep"], key=lambda x: x["threshold"])]
    assert approvals == sorted(approvals, reverse=True)
    # at threshold 1.0 nothing is confident enough to auto-approve
    assert cal["sweep"][-1]["threshold"] == 1.0
    assert cal["sweep"][-1]["approve_ready"] == 0


def test_sweep_matches_persisted_verdicts_at_current_threshold(make):
    svc = _load(make)
    cal = calibration.threshold_sweep(svc.pipeline)
    current = next(r for r in cal["sweep"] if abs(r["threshold"] - svc.pipeline.ai_threshold) < 1e-9)
    stored_approve = sum(1 for l in svc.repo.list_leads() if l.get("combined_verdict") == "approve_ready")
    stored_manual = sum(1 for l in svc.repo.list_leads() if l.get("combined_verdict") == "manual_review")
    stored_reject = sum(1 for l in svc.repo.list_leads() if l.get("combined_verdict") == "reject")
    assert (current["approve_ready"], current["manual_review"], current["reject"]) == (
        stored_approve, stored_manual, stored_reject
    )


def test_compute_shape(make):
    svc = _load(make)
    out = calibration.compute(svc.pipeline)
    assert set(out) == {"tenant_id", "disagreement", "calibration"}
    assert out["tenant_id"] == svc.tenant_id
