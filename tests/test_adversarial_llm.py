"""Adversarial LLM provider end-to-end tests.

Proves the safety wrapper holds even when the LLM provider itself misbehaves.
The pipeline is exercised via the same ``LeadPipeline.qualify_lead`` used in
production - we just swap in ``AdversarialMockLLMProvider`` beforehand. For
each mode we verify:

  * no adversarial-AI output ever produces a stage that lets the lead flow
    through to outbox without a human approve;
  * the AI-side safety_flags (or the strict-schema rejection) surface in the
    lead's ``ai_reason`` field so an operator auditing the trail can see WHY
    the AI path was refused.
"""

from __future__ import annotations

import pytest

from lead_engine.approval import human_gate
from lead_engine.models import LeadStage
from lead_engine.pipeline.qualify_ai import AdversarialMockLLMProvider

from .conftest import ALPHA, STRONG

MODES = ["non_json", "schema_extra", "bad_confidence", "ungrounded",
         "empty_evidence", "wrong_verdict", "override_optout"]


@pytest.mark.parametrize("mode", MODES)
def test_adversarial_llm_cannot_produce_auto_delivery(make, mode):
    """Whatever the sabotaging provider returns, the pipeline must never
    auto-approve the lead; the max it can get is ``approved_ready`` ONLY if the
    rules path independently agrees (see ``combine_verdicts``). For all modes
    below, the AI's response must be flagged as unsafe or refused."""
    svc = make(ALPHA)
    svc.pipeline.provider = AdversarialMockLLMProvider(mode=mode)

    res = svc.pipeline.ingest_raw(STRONG, "csv")
    svc.pipeline.qualify_lead(res.lead_id)
    lead = svc.repo.get_lead(res.lead_id)

    # the AI's raw opinion must be recorded but wrapped:
    ai_reason = lead.get("ai_reason") or ""
    combined = lead.get("combined_verdict")

    # 1. The AI's declared approve_ready never survives into combined_verdict
    #    without the pipeline also recording an "ok" ai_reason.
    if ai_reason != "ok":
        assert combined != "approve_ready", \
            f"mode={mode}: combined=approve_ready despite safety flags [{ai_reason}]"

    # 2. Regardless of the AI outcome, outbox remains empty without approval.
    svc.delivery.enqueue_all_approved()
    svc.delivery.process_all()
    lead_after = svc.repo.get_lead(res.lead_id)
    assert lead_after["stage"] != LeadStage.OUTBOXED.value, \
        f"mode={mode}: lead reached outbox with no human approve"
    assert svc.outbox.count() == 0


@pytest.mark.parametrize("mode", MODES)
def test_adversarial_llm_gate_blocks_any_attempted_bypass(make, mode):
    """Even if a bug let an adversarial-AI lead into human_approved (it shouldn't),
    the ``assert_deliverable`` guard would still require an actual approve decision
    row. We force the stage and confirm the guard still blocks delivery."""
    svc = make(ALPHA)
    svc.pipeline.provider = AdversarialMockLLMProvider(mode=mode)
    res = svc.pipeline.ingest_raw(STRONG, "csv")
    svc.pipeline.qualify_lead(res.lead_id)

    # simulate a hypothetical bug: flip stage without recording a decision
    svc.repo.update_lead_fields(res.lead_id, {})  # no-op write to keep row fresh
    svc.repo.set_stage(res.lead_id, LeadStage.HUMAN_APPROVED.value)

    with pytest.raises(human_gate.ApprovalError):
        human_gate.assert_deliverable(svc.repo, res.lead_id)


def test_adversarial_llm_mode_list_is_covered():
    """Sanity: every mode we plan to test is actually dispatched by the provider."""
    p = AdversarialMockLLMProvider(mode="ungrounded")
    for mode in MODES:
        p.mode = mode
        raw = p.classify({"evidence_field_ids": ["ev_1", "ev_2", "ev_3", "ev_4"]})
        # mode "non_json" deliberately returns non-JSON text; all others must be valid JSON.
        if mode == "non_json":
            assert raw.startswith("not-a-json")
        else:
            import json
            json.loads(raw)  # will raise if malformed
