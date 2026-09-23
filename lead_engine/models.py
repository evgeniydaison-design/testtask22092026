"""Domain enums and strict schemas.

The pipeline stage machine (``LeadStage``) is the single source of truth for
what is allowed next. Delivery-related stages can only be reached through
``HUMAN_APPROVED`` - this is what guarantees requirement 4 (a draft can never
reach the outbox without an explicit human approval).
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LeadStage(str, enum.Enum):
    RECEIVED = "received"
    NORMALIZED = "normalized"
    DEDUPED = "deduped"
    APPROVED_READY = "approved_ready"      # rules + AI agree, auto-pass to queue
    MANUAL_REVIEW = "manual_review"        # needs a human decision
    REJECTED = "rejected"                  # opt-out / hard reject (no delivery)
    HUMAN_APPROVED = "human_approved"      # explicit human go-ahead
    HUMAN_REJECTED = "human_rejected"      # explicit human rejection
    CRM_SYNCED = "crm_synced"              # mock CRM upsert succeeded
    DLQ = "dlq"                            # delivery exhausted retries -> DLQ
    OUTBOXED = "outboxed"                  # approved draft recorded in outbox


# Stages that must never be entered without a preceding human approval.
POST_APPROVAL_STAGES = {
    LeadStage.CRM_SYNCED,
    LeadStage.OUTBOXED,
}

VALID_TRANSITIONS: dict[LeadStage, set[LeadStage]] = {
    LeadStage.RECEIVED: {LeadStage.NORMALIZED},
    LeadStage.NORMALIZED: {LeadStage.DEDUPED},
    LeadStage.DEDUPED: {
        LeadStage.APPROVED_READY,
        LeadStage.MANUAL_REVIEW,
        LeadStage.REJECTED,
    },
    LeadStage.APPROVED_READY: {LeadStage.MANUAL_REVIEW, LeadStage.HUMAN_APPROVED},
    LeadStage.MANUAL_REVIEW: {LeadStage.HUMAN_APPROVED, LeadStage.HUMAN_REJECTED},
    LeadStage.HUMAN_APPROVED: {LeadStage.CRM_SYNCED, LeadStage.DLQ},
    LeadStage.CRM_SYNCED: {LeadStage.OUTBOXED},
    LeadStage.DLQ: {LeadStage.CRM_SYNCED},  # only via reprocess + retry success
    # Terminal stages:
    LeadStage.REJECTED: set(),
    LeadStage.HUMAN_REJECTED: set(),
    LeadStage.OUTBOXED: set(),
}


def can_transition(frm: LeadStage | str, to: LeadStage | str) -> bool:
    """Return True if moving a lead from stage ``frm`` to ``to`` is legal."""
    frm = LeadStage(frm) if not isinstance(frm, LeadStage) else frm
    to = LeadStage(to) if not isinstance(to, LeadStage) else to
    return to in VALID_TRANSITIONS.get(frm, set())


def assert_transition(frm: LeadStage | str, to: LeadStage | str) -> None:
    if not can_transition(frm, to):
        raise InvalidTransition(
            f"Illegal lead stage transition: {frm} -> {to}"
        )


class InvalidTransition(RuntimeError):
    pass


class Verdict(str, enum.Enum):
    APPROVE_READY = "approve_ready"
    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"


class SourceType(str, enum.Enum):
    CSV = "csv"
    JSON = "json"
    WEBHOOK = "webhook"


class DeliveryState(str, enum.Enum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    DLQ = "dlq"


# --------------------------------------------------------------------------
# Strict AI output schema (requirement 3).
#   * extra="forbid"     -> any field outside the schema is a hard error
#   * confidence         -> bounded [0, 1]
#   * evidence_field_ids -> the only thing AI may reference (grounding)
# The adapter in pipeline/qualify_ai.py enforces grounding + injection safety.
# --------------------------------------------------------------------------
AIVerdict = Literal["approve_ready", "manual_review", "reject"]

_ALLOWED_VERDICTS = {"approve_ready", "manual_review", "reject"}


class AIQualification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: AIVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_field_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=280)

    @field_validator("verdict")
    @classmethod
    def _verdict_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_VERDICTS:
            raise ValueError("verdict out of allowed set")
        return v


# Inbound mock webhook payload (requirement 1/2: tenant-scoped + idempotent).
class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown keys preserved as evidence

    tenant_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    record: dict = Field(default_factory=dict)
