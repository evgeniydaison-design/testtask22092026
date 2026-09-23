"""Retry / DLQ / reprocess engine (requirement 5, the deep module).

A :class:`DeliveryService` turns a human-approved lead into a durable
``delivery_op`` and drives it through the mock CRM with:
  * exponential backoff + jitter for retryable failures (429 / 5xx / timeout)
  * immediate dead-lettering for non-retryable failures (4xx)
  * a bounded number of attempts, then DLQ
  * idempotency: the op's idempotency key is stable per lead, so a reprocess of
    an op that actually succeeded upstream is a no-op (no duplicate CRM rows,
    no duplicate outbox entries)

``sleep`` is injectable so tests run instantly without real waiting.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..approval.human_gate import assert_deliverable
from ..db import LeadRepository
from ..models import DeliveryState, LeadStage, assert_transition
from .crm_mock import NON_RETRYABLE, RETRYABLE, MockCRM
from .outbox import MockOutbox


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_seconds: float = 0.5
    max_seconds: float = 30.0
    jitter: float = 0.2

    def backoff(self, attempt: int) -> float:
        """attempt is 1-based. Exponential with full-ish jitter, capped."""
        raw = self.base_seconds * (2 ** (attempt - 1))
        capped = min(raw, self.max_seconds)
        jitter = capped * self.jitter * random.random()
        return round(capped + jitter, 4)


def is_retryable(status: int) -> bool:
    if status in RETRYABLE:
        return True
    if status in NON_RETRYABLE:
        return False
    return 500 <= status < 600  # unknown 5xx -> retryable by default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class DeliveryService:
    def __init__(
        self,
        repo: LeadRepository,
        crm: MockCRM,
        outbox: MockOutbox,
        *,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.repo = repo
        self.crm = crm
        self.outbox = outbox
        self.policy = policy or RetryPolicy()
        # injectable sleep -> instant in tests
        self._sleep = sleep or (lambda s: None)

    # -- op creation --------------------------------------------------------
    def _op_key(self, lead_id: str) -> str:
        return f"crm_sync:{self.repo.tenant_id}:{lead_id}"

    def enqueue(self, lead_id: str) -> dict:
        # gate: only an approved lead may even create a delivery op
        assert_deliverable(self.repo, lead_id)
        key = self._op_key(lead_id)
        return self.repo.create_delivery_op(
            lead_id, "crm_sync", key, max_attempts=self.policy.max_attempts
        )

    # -- single op processing ----------------------------------------------
    def process_op(self, op_id: str) -> dict:
        op = self.repo.get_delivery_op(op_id)
        if op is None:
            raise KeyError(f"delivery op {op_id} not found")
        if op["state"] == DeliveryState.SUCCEEDED.value:
            return op  # already done (idempotent)

        lead_id = op["lead_id"]
        # gate re-checked every attempt: delivery must remain authorized
        lead = assert_deliverable(self.repo, lead_id)

        attempts = op["attempts"]
        while attempts < op["max_attempts"]:
            attempts += 1
            result = self.crm.upsert_contact_and_task(
                idempotency_key=op["idempotency_key"],
                contact={
                    "email": lead.get("email") or f"{lead_id}@noreply.invalid",
                    "name": lead.get("name"),
                    "company": lead.get("company"),
                    "country": lead.get("country"),
                },
                task=self._task_for(lead),
            )
            self.repo.update_delivery_op(op_id, {"attempts": attempts, "last_error": None})

            if result.ok:
                self._on_success(op_id, lead_id, replayed=bool(result.body.get("replayed")))
                return self.repo.get_delivery_op(op_id)  # type: ignore[return-value]

            if not is_retryable(result.status):
                self._on_dlq(op_id, lead_id, f"non-retryable HTTP {result.status}", retryable=False)
                return self.repo.get_delivery_op(op_id)  # type: ignore[return-value]

            # retryable -> backoff, unless we've exhausted attempts
            if attempts >= op["max_attempts"]:
                self._on_dlq(op_id, lead_id, f"exhausted attempts (last HTTP {result.status})", retryable=True)
                return self.repo.get_delivery_op(op_id)  # type: ignore[return-value]

            self.repo.update_delivery_op(
                op_id,
                {"state": DeliveryState.RETRYING.value, "last_error": f"HTTP {result.status}",
                 "next_retry_at": _iso(_now() + timedelta(seconds=self.policy.backoff(attempts)))},
            )
            self._sleep(self.policy.backoff(attempts))

        # defensive: loop exits only via return, but keep a terminal fallback
        self._on_dlq(op_id, lead_id, "no attempts available", retryable=False)
        return self.repo.get_delivery_op(op_id)  # type: ignore[return-value]

    def _task_for(self, lead: dict) -> dict:
        draft = self.repo.get_draft_for_lead(lead["id"])
        return {
            "owner": "ops-queue",
            "subject": (draft or {}).get("subject", "Follow up approved lead"),
            "body": (draft or {}).get("body", ""),
            "due": _iso(_now() + timedelta(days=2)),
        }

    def _on_success(self, op_id: str, lead_id: str, *, replayed: bool) -> None:
        lead = self.repo.get_lead(lead_id)
        assert lead is not None
        # Defensive: a reprocess normally resets dlq->human_approved first; if
        # we ever land here still in dlq, restore the approved stage.
        if lead["stage"] == LeadStage.DLQ.value:
            self.repo.set_stage(lead_id, LeadStage.HUMAN_APPROVED.value)
            lead = self.repo.get_lead(lead_id)
        # Outbox is guarded by the human-approval gate, so enqueue it while the
        # lead is still human_approved (idempotent: no duplicate on replay).
        self.outbox.write(self.repo, lead_id)
        # Now advance the stage machine: human_approved -> crm_synced -> outboxed
        if lead and lead["stage"] == LeadStage.HUMAN_APPROVED.value:
            assert_transition(lead["stage"], LeadStage.CRM_SYNCED.value)
            self.repo.set_stage(lead_id, LeadStage.CRM_SYNCED.value)
        lead = self.repo.get_lead(lead_id)
        if lead and lead["stage"] == LeadStage.CRM_SYNCED.value:
            assert_transition(lead["stage"], LeadStage.OUTBOXED.value)
            self.repo.set_stage(lead_id, LeadStage.OUTBOXED.value)
        self.repo.update_delivery_op(op_id, {"state": DeliveryState.SUCCEEDED.value, "last_error": None})
        self.repo.resolve_dlq(op_id)
        self.repo.add_event(lead_id, "delivery_success", f"replayed={replayed}")

    def _on_dlq(self, op_id: str, lead_id: str, reason: str, *, retryable: bool) -> None:
        self.repo.update_delivery_op(op_id, {"state": DeliveryState.DLQ.value, "last_error": reason})
        lead = self.repo.get_lead(lead_id)
        if lead and lead["stage"] in (LeadStage.HUMAN_APPROVED.value, LeadStage.CRM_SYNCED.value):
            if lead["stage"] == LeadStage.HUMAN_APPROVED.value:
                assert_transition(lead["stage"], LeadStage.DLQ.value)
            self.repo.set_stage(lead_id, LeadStage.DLQ.value)
        self.repo.add_dlq(op_id, lead_id, reason, error_profile=self.crm.error_profile)
        self.repo.add_event(lead_id, "delivery_dlq", reason)

    # -- bulk helpers -------------------------------------------------------
    def enqueue_all_approved(self) -> list[dict]:
        ops = []
        for lead in self.repo.list_leads(stage=LeadStage.HUMAN_APPROVED.value):
            ops.append(self.enqueue(lead["id"]))
        return ops

    def process_all(self, state: str = DeliveryState.PENDING.value) -> dict:
        summary = {"succeeded": 0, "dlq": 0, "skipped": 0}
        for op in self.repo.list_delivery_ops(state=state):
            res = self.process_op(op["id"])
            if res["state"] == DeliveryState.SUCCEEDED.value:
                summary["succeeded"] += 1
            elif res["state"] == DeliveryState.DLQ.value:
                summary["dlq"] += 1
            else:
                summary["skipped"] += 1
        return summary

    def reprocess_dlq(self) -> dict:
        """Move DLQ ops back to pending and retry them. Idempotency guarantees a
        lead that actually landed upstream is not duplicated."""
        summary = {"reprocessed": 0, "succeeded": 0, "still_dlq": 0}
        for entry in self.repo.list_dlq(status="open"):
            op = self.repo.get_delivery_op(entry["op_id"])
            if op is None:
                continue
            # reset lead stage dlq -> human_approved so it can be re-driven
            lead = self.repo.get_lead(op["lead_id"])
            if lead and lead["stage"] == LeadStage.DLQ.value:
                self.repo.set_stage(op["lead_id"], LeadStage.HUMAN_APPROVED.value)
            self.repo.update_delivery_op(op["id"], {"state": DeliveryState.PENDING.value, "attempts": 0})
            summary["reprocessed"] += 1
            res = self.process_op(op["id"])
            if res["state"] == DeliveryState.SUCCEEDED.value:
                summary["succeeded"] += 1
            else:
                summary["still_dlq"] += 1
        return summary
