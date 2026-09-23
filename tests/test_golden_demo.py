"""Golden-file / snapshot test for reproducibility.

Runs the actual ``cli demo`` command for both tenants in an isolated temp data
dir, parses the JSON report from stdout, and asserts the metrics match the
committed golden dict exactly (except the SLA timings which are naturally
non-deterministic). This is a much stronger claim than "the code doesn't
crash": it locks in "one repo + one command = THESE numbers".

If a code change intentionally moves the funnel (e.g. a new safety rule that
turns 2 previously outboxed leads into manual_review), this test will fail and
force an update to the golden dict - which is exactly what you want in review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lead_engine import cli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# Deterministic subset of the metrics dict the demo prints for each tenant.
GOLDEN = {
    "tenant_alpha": {
        "sources": 51,
        "leads_total": 46,
        "duplicates_merged": 5,
        "stage_counts": {"manual_review": 17, "outboxed": 25, "rejected": 4},
        "verdicts": {"manual_review": 17, "approve_ready": 25, "reject": 4},
        "ai_safety_flagged": 11,
        "manual_review_rate": 0.405,
        "delivery": {"succeeded": 25, "pending": 0, "retrying": 0, "dlq": 0},
        "crm_status_histogram": {"200": 25},
        "outbox_entries": 25,
    },
    "tenant_beta": {
        "sources": 51,
        "leads_total": 46,
        "duplicates_merged": 5,
        "stage_counts": {"manual_review": 17, "outboxed": 25, "rejected": 4},
        "verdicts": {"manual_review": 17, "approve_ready": 25, "reject": 4},
        "ai_safety_flagged": 11,
        "manual_review_rate": 0.405,
        "delivery": {"succeeded": 25, "pending": 0, "retrying": 0, "dlq": 0},
        "crm_status_histogram": {"200": 25},
        "outbox_entries": 25,
    },
}


@pytest.mark.parametrize("tenant", ["tenant_alpha", "tenant_beta"])
def test_demo_metrics_match_golden(tenant, tmp_path, capsys, monkeypatch):
    """Fresh data dir -> run demo -> compare funnel to the golden dict."""
    import io, contextlib
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ATHENAI_DATA_DIR", str(data_dir))
    # re-point the settings cache so build_service uses our tmp dir
    import lead_engine.config as cfg
    if hasattr(cfg, "_CACHED_SETTINGS"):
        cfg._CACHED_SETTINGS = None

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["demo", "--tenant", tenant])
    assert code == 0, f"demo for {tenant} returned {code}"

    payload = json.loads(buf.getvalue())
    metrics = payload["metrics"]
    # keep only deterministic keys from the golden view
    subset = {k: metrics[k] for k in GOLDEN[tenant]}
    assert subset == GOLDEN[tenant], (
        f"demo drift for {tenant}:\n"
        f"  expected: {json.dumps(GOLDEN[tenant], sort_keys=True)}\n"
        f"  got     : {json.dumps(subset, sort_keys=True)}"
    )


def test_demo_is_reproducible_two_runs_same_numbers(tmp_path, monkeypatch):
    """Second run against a fresh dir must produce identical numbers to the first."""
    import io, contextlib
    runs = []
    for _ in range(2):
        data_dir = tmp_path / f"data{len(runs)}"
        monkeypatch.setenv("ATHENAI_DATA_DIR", str(data_dir))
        import lead_engine.config as cfg
        if hasattr(cfg, "_CACHED_SETTINGS"):
            cfg._CACHED_SETTINGS = None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["demo", "--tenant", "tenant_alpha"])
        m = json.loads(buf.getvalue())["metrics"]
        # strip non-deterministic timing fields
        m.pop("avg_seconds_ingest_to_decision", None)
        m.pop("avg_seconds_in_manual_review", None)
        runs.append(m)
    assert runs[0] == runs[1], f"two fresh demo runs disagree: {runs[0]} vs {runs[1]}"
