"""Human approval gate tests (requirement 4). The gate is the only path to
delivery: unapproved drafts must never reach CRM or the outbox."""

from __future__ import annotations

import pytest

from lead_engine.approval import human_gate
from lead_engine.models import LeadStage, assert_transition, can_transition

from .conftest import ALPHA, OPTOUT, STRONG, THIN


def _qualify(svc, raw, hint=None):
    res = svc.pipeline.ingest_raw(raw, "csv", idempotency_hint=hint)
    svc.pipeline.qualify_lead(res.lead_id)
    return res.lead_id


def test_unapproved_lead_cannot_be_delivered(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, STRONG)   # approved_ready, but no human decision yet
    with pytest.raises(human_gate.ApprovalError):
        svc.delivery.enqueue(lead_id)


def test_outbox_refuses_unapproved_directly(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, STRONG)
    with pytest.raises(human_gate.ApprovalError):
        svc.outbox.write(svc.repo, lead_id)


def test_optout_lead_cannot_be_approved(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, OPTOUT)
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.REJECTED.value
    with pytest.raises(human_gate.ApprovalError):
        human_gate.approve_lead(svc.repo, lead_id, actor="ops-test")


def test_approve_then_deliver_happy_path(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, STRONG)
    human_gate.approve_lead(svc.repo, lead_id, actor="ops-test", note="lgtm")
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.HUMAN_APPROVED.value

    op = svc.delivery.enqueue(lead_id)
    result = svc.delivery.process_op(op["id"])
    assert result["state"] == "succeeded"

    lead = svc.repo.get_lead(lead_id)
    assert lead["stage"] == LeadStage.OUTBOXED.value
    assert svc.crm.get_contact_by_email(STRONG["email"]) is not None
    assert svc.crm.count_tasks() == 1                    # a follow-up task was made
    entries = svc.outbox.read_all()
    assert len(entries) == 1
    assert entries[0]["lead_id"] == lead_id
    assert entries[0]["delivery"] == "MOCK_LOCAL_ONLY_NOT_SENT"


def test_reject_records_decision_and_blocks_delivery(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, THIN)     # manual_review
    human_gate.reject_lead(svc.repo, lead_id, actor="ops-test", note="not qualified")
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.HUMAN_REJECTED.value
    decisions = svc.repo.list_decisions(lead_id)
    assert any(d["action"] == "reject" for d in decisions)
    with pytest.raises(human_gate.ApprovalError):
        svc.delivery.enqueue(lead_id)


def test_edit_then_approve_keeps_gate(make):
    svc = make(ALPHA)
    lead_id = _qualify(svc, STRONG)
    human_gate.edit_and_approve(svc.repo, lead_id, actor="ops-test", new_body="Edited follow-up body")
    lead = svc.repo.get_lead(lead_id)
    assert lead["stage"] == LeadStage.HUMAN_APPROVED.value
    assert svc.repo.get_draft_for_lead(lead_id)["body"] == "Edited follow-up body"


def test_stage_machine_blocks_illegal_transitions():
    # the state machine is the structural reason an unapproved lead cannot
    # reach CRM/outbox: there is simply no legal edge into those stages except
    # via human_approved.
    assert can_transition(LeadStage.MANUAL_REVIEW, LeadStage.OUTBOXED) is False
    assert can_transition(LeadStage.APPROVED_READY, LeadStage.CRM_SYNCED) is False
    assert can_transition(LeadStage.HUMAN_APPROVED, LeadStage.CRM_SYNCED) is True
    assert can_transition(LeadStage.CRM_SYNCED, LeadStage.OUTBOXED) is True
    with pytest.raises(Exception):
        assert_transition(LeadStage.MANUAL_REVIEW, LeadStage.OUTBOXED)
