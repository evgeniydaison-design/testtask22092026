"""Unit tests for normalization, dedup, rules + AI qualification and drafts."""

from __future__ import annotations

import json

from lead_engine.pipeline import normalize as nz
from lead_engine.pipeline.draft import build_draft, validate_template
from lead_engine.pipeline.qualify_ai import (
    MockLLMProvider,
    combine_verdicts,
    qualify_ai,
)
from lead_engine.pipeline.qualify_rules import Verdict, qualify_rules

from .conftest import INJECTION, OPTOUT, STRONG, THIN


class StaticProvider:
    """Test provider returning a fixed raw JSON string (can be malformed)."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def classify(self, payload: dict) -> str:
        return self._raw


# --- normalization --------------------------------------------------------
def test_normalize_email_gmail_plus_and_dots():
    assert nz.normalize_email("Ava.Strong+lead@Gmail.com ") == "avastrong@gmail.com"
    assert nz.normalize_email("John.Doe@Example.com") == "john.doe@example.com"


def test_normalize_phone_and_canonical_key():
    assert nz.normalize_phone("1 (555) 000-1234") == "+15550001234"
    lead = nz.normalize(STRONG)
    assert lead["canonical_key"] == "email:" + lead["email"]
    # anonymous record still yields a deterministic key
    anon = nz.normalize({"notes": "hi"})
    assert anon["canonical_key"].startswith("anon:")
    assert anon["canonical_key"] == nz.normalize({"notes": "hi"})["canonical_key"]


def test_normalize_flags_safety_cases():
    assert nz.normalize(OPTOUT)["opt_out"] == 1
    assert nz.normalize(INJECTION)["injection_flag"] == 1
    conflict = dict(STRONG, alt_emails="totally@other.com", conflict="true")
    assert nz.normalize(conflict)["conflict_flag"] == 1


# --- rules qualification --------------------------------------------------
def test_rules_approve_ready_for_strong_lead():
    assert qualify_rules(nz.normalize(STRONG)).verdict == Verdict.APPROVE_READY.value


def test_rules_manual_for_thin_lead():
    assert qualify_rules(nz.normalize(THIN)).verdict == Verdict.MANUAL_REVIEW.value


def test_rules_reject_optout():
    assert qualify_rules(nz.normalize(OPTOUT)).verdict == Verdict.REJECT.value


def test_rules_manual_for_injection_and_conflict():
    assert qualify_rules(nz.normalize(INJECTION)).verdict == Verdict.MANUAL_REVIEW.value
    conflict = nz.normalize(dict(STRONG, conflict="true"))
    assert qualify_rules(conflict).verdict == Verdict.MANUAL_REVIEW.value


# --- AI adapter strictness + safety --------------------------------------
def test_ai_rejects_extra_field_schema():
    raw = json.dumps({
        "verdict": "approve_ready", "confidence": 0.9,
        "evidence_field_ids": ["ev_name"], "reason": "ok", "sql": "drop table",
    })
    res = qualify_ai(nz.normalize(STRONG), StaticProvider(raw))
    assert res.schema_ok is False
    assert res.verdict == Verdict.MANUAL_REVIEW.value
    assert any("schema_violation" in f for f in res.safety_flags)


def test_ai_rejects_out_of_range_confidence_and_bad_verdict():
    bad_conf = json.dumps({"verdict": "approve_ready", "confidence": 5,
                           "evidence_field_ids": ["ev_name"], "reason": "x"})
    assert qualify_ai(nz.normalize(STRONG), StaticProvider(bad_conf)).schema_ok is False
    bad_verdict = json.dumps({"verdict": "auto_send", "confidence": 0.9,
                              "evidence_field_ids": ["ev_name"], "reason": "x"})
    assert qualify_ai(nz.normalize(STRONG), StaticProvider(bad_verdict)).schema_ok is False


def test_ai_non_json_output_forced_manual():
    assert qualify_ai(nz.normalize(STRONG), StaticProvider("not json")).verdict == Verdict.MANUAL_REVIEW.value


def test_ai_ungrounded_evidence_reference_is_flagged():
    raw = json.dumps({"verdict": "approve_ready", "confidence": 0.95,
                      "evidence_field_ids": ["ev_password", "ev_credit_card"], "reason": "x"})
    res = qualify_ai(nz.normalize(STRONG), StaticProvider(raw))
    assert res.grounded is False
    assert res.verdict == Verdict.MANUAL_REVIEW.value
    assert any(f.startswith("ungrounded_refs") for f in res.safety_flags)


def test_ai_disagreement_forces_manual():
    # rules would approve STRONG, but a schema-valid AI says reject -> human decides
    raw = json.dumps({"verdict": "reject", "confidence": 0.9,
                      "evidence_field_ids": ["ev_name"], "reason": "not a fit"})
    res = qualify_ai(nz.normalize(STRONG), StaticProvider(raw))
    assert combine_verdicts(Verdict.APPROVE_READY.value, res) == Verdict.MANUAL_REVIEW.value


def test_ai_cannot_auto_approve_injection_lead():
    res = qualify_ai(nz.normalize(INJECTION), MockLLMProvider())
    assert res.verdict == Verdict.MANUAL_REVIEW.value
    assert "lead_injection_flagged" in res.safety_flags


def test_combine_approve_only_when_rules_and_ai_agree():
    ai = qualify_ai(nz.normalize(STRONG), MockLLMProvider())
    assert combine_verdicts(Verdict.APPROVE_READY.value, ai) == Verdict.APPROVE_READY.value
    assert combine_verdicts(Verdict.REJECT.value, ai) == Verdict.REJECT.value


# --- evidence-only draft --------------------------------------------------
def test_draft_uses_only_evidence_fields():
    lead = nz.normalize(STRONG)
    draft = build_draft(lead)
    assert lead["name"] in draft.body
    assert lead["company"] in draft.body
    # no fabricated specifics
    assert "$" not in draft.body
    assert "free" not in draft.body.lower()


def test_draft_quarantines_injection_lead():
    lead = nz.normalize(INJECTION)
    draft = build_draft(lead)
    assert draft.safe is False
    # the untrusted inbound instruction must NOT be echoed into the draft
    assert "ignore all previous instructions" not in draft.body.lower()
    assert "Quarantined" in draft.body


def test_template_only_uses_allowed_placeholders():
    validate_template()  # raises AssertionError on any disallowed token
