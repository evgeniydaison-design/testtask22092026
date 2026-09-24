"""Fuzz invariant: adversarial input NEVER reaches a delivery stage/outbox."""

from __future__ import annotations

from lead_engine import fuzz


def test_fuzz_invariant_holds(tmp_path):
    rep = fuzz.run_fuzz(tmp_path / "fz", n=400, seed=7)
    assert rep.invariant_ok, f"violations: {rep.violations}"
    assert rep.outbox_entries == 0
    # the generator actually exercised the safety paths
    assert rep.flags["injection"] > 0
    assert rep.flags["opt_out"] > 0
    # every unapproved outbox write was refused by the gate
    assert rep.unapproved_writes_blocked == rep.unapproved_write_attempts
    # no lead advanced to a delivery stage without a human
    for stage in ("human_approved", "crm_synced", "outboxed", "dlq"):
        assert rep.stage_counts.get(stage, 0) == 0


def test_fuzz_is_reproducible(tmp_path):
    a = fuzz.run_fuzz(tmp_path / "a", n=200, seed=99).as_dict()
    b = fuzz.run_fuzz(tmp_path / "b", n=200, seed=99).as_dict()
    assert a == b


def test_fuzz_other_seed_still_safe(tmp_path):
    rep = fuzz.run_fuzz(tmp_path / "fz2", n=250, seed=1234)
    assert rep.invariant_ok, rep.violations
