"""Integration-resilience tests (requirement 5, the deep module): retry with
backoff for 429/5xx, dead-lettering, reprocess, and idempotent delivery."""

from __future__ import annotations

from lead_engine.approval import human_gate
from lead_engine.delivery.retry import RetryPolicy, is_retryable
from lead_engine.models import DeliveryState, LeadStage

from .conftest import ALPHA, STRONG


def _approved_lead(svc):
    """Ingest + qualify an approve_ready lead, then have a human approve it."""
    res = svc.pipeline.ingest_raw(STRONG, "csv")
    svc.pipeline.qualify_lead(res.lead_id)
    lead = svc.repo.get_lead(res.lead_id)
    assert lead["stage"] == LeadStage.APPROVED_READY.value, lead["stage"]
    human_gate.approve_lead(svc.repo, res.lead_id, actor="ops-test")
    return res.lead_id


def test_backoff_is_exponential_and_capped():
    p = RetryPolicy(max_attempts=8, base_seconds=1.0, max_seconds=10.0, jitter=0.0)
    seq = [p.backoff(a) for a in range(1, 8)]
    assert seq == sorted(seq)                 # monotonically non-decreasing
    assert seq[-1] <= p.max_seconds           # capped


def test_retryable_classification():
    assert is_retryable(429) and is_retryable(503) and is_retryable(500)
    assert not is_retryable(404) and not is_retryable(405) and not is_retryable(422)


def test_retry_429_then_success(make):
    svc = make(ALPHA, crm_error_profile="flaky_429")   # fails twice, then 200
    lead_id = _approved_lead(svc)
    op = svc.delivery.enqueue(lead_id)
    result = svc.delivery.process_op(op["id"])
    assert result["state"] == DeliveryState.SUCCEEDED.value
    assert result["attempts"] == 3                      # two 429s + one success
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.OUTBOXED.value


def test_retry_5xx_exhausts_then_dlq(make):
    svc = make(ALPHA, crm_error_profile="always_500")
    lead_id = _approved_lead(svc)
    op = svc.delivery.enqueue(lead_id)
    result = svc.delivery.process_op(op["id"])
    assert result["state"] == DeliveryState.DLQ.value
    assert result["attempts"] == svc.delivery.policy.max_attempts
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.DLQ.value
    assert len(svc.repo.list_dlq("open")) == 1
    assert svc.outbox.count() == 0                      # nothing delivered


def test_non_retryable_goes_straight_to_dlq(make):
    svc = make(ALPHA, crm_error_profile="non_retryable_405")
    lead_id = _approved_lead(svc)
    op = svc.delivery.enqueue(lead_id)
    result = svc.delivery.process_op(op["id"])
    assert result["state"] == DeliveryState.DLQ.value
    assert result["attempts"] == 1                      # no retries on 4xx


def test_reprocess_after_recovery_no_duplicates(make):
    svc = make(ALPHA, crm_error_profile="always_500")
    lead_id = _approved_lead(svc)
    op = svc.delivery.enqueue(lead_id)
    svc.delivery.process_op(op["id"])
    assert svc.repo.get_delivery_op(op["id"])["state"] == DeliveryState.DLQ.value

    # upstream recovers, then reprocess drains the DLQ
    svc.crm.error_profile = "none"
    summary = svc.delivery.reprocess_dlq()
    assert summary["succeeded"] == 1
    assert svc.repo.get_delivery_op(op["id"])["state"] == DeliveryState.SUCCEEDED.value
    assert svc.repo.get_lead(lead_id)["stage"] == LeadStage.OUTBOXED.value
    assert svc.crm.count_contacts() == 1                # exactly one contact
    assert svc.outbox.count() == 1                       # exactly one outbox entry


def test_idempotent_delivery_replay(make):
    svc = make(ALPHA)
    lead_id = _approved_lead(svc)
    op = svc.delivery.enqueue(lead_id)
    svc.delivery.process_op(op["id"])
    assert svc.crm.count_contacts() == 1

    # replaying the same idempotency key upstream is a no-op (no 2nd contact)
    key = op["idempotency_key"]
    resp = svc.crm.upsert_contact_and_task(
        idempotency_key=key, contact={"email": STRONG["email"]}, task={"owner": "x"}
    )
    assert resp.ok and resp.body.get("replayed") is True
    assert svc.crm.count_contacts() == 1

    # re-processing an already-succeeded op leaves everything unchanged
    svc.delivery.process_op(op["id"])
    assert svc.outbox.count() == 1
