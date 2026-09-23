"""Lead trail / audit view.

``explain_lead`` composes everything the system has persisted about one lead:
raw source envelopes, normalized identity + flags, rules verdict + reason,
AI verdict + confidence + safety reason, combined verdict, human decisions,
delivery attempts (+ DLQ reason when applicable), and the final outbox entry.
This is the operator-facing "one button explains the whole journey" view -
useful for audits, incident review, and grading the safety loop.
"""

from __future__ import annotations

from .approval import human_gate
from .db import LeadRepository
from .delivery.crm_mock import MockCRM
from .delivery.outbox import MockOutbox
from .models import LeadStage


def explain_lead(repo: LeadRepository, crm: MockCRM, outbox: MockOutbox, lead_id: str) -> dict:
    """Return a single JSON-serialisable dict describing the full lead trail.

    Raises ``human_gate.ApprovalError`` if the lead does not exist in this
    tenant (tenant isolation is enforced by the repository itself).
    """
    lead = repo.get_lead(lead_id)
    if lead is None:
        raise human_gate.ApprovalError(f"lead {lead_id} not found in tenant {repo.tenant_id}")

    # ---- raw source envelopes (already parsed dicts, keyed as "raw") ----
    sources_raw = repo.get_sources_for_lead(lead_id)
    sources = [{
        "source_type": s.get("source_type"),
        "idempotency_key": s.get("idempotency_key"),
        "received_at": s.get("collected_at"),
        "raw": s.get("raw"),
    } for s in sources_raw]

    # ---- draft + decisions + full stage-transition trail ----
    draft = repo.get_draft_for_lead(lead_id)
    decisions = repo.list_decisions(lead_id)
    events = repo.list_events_for_lead(lead_id)

    # ---- delivery attempts, enriched with DLQ row if any ----
    ops_for_lead = [op for op in repo.list_delivery_ops() if op.get("lead_id") == lead_id]
    all_dlq = repo.list_dlq(status=None)
    dlq_by_op = {d.get("op_id"): d for d in all_dlq}
    delivery_detail: list[dict] = []
    for op in ops_for_lead:
        row = dict(op)
        d = dlq_by_op.get(row.get("id"))
        if d is not None:
            row["dlq"] = {
                "reason": d.get("reason"),
                "status": d.get("status"),
                "created_at": d.get("created_at"),
            }
        # attach the per-key crm status trail if the mock exposes it
        hist = crm.status_history_for_key(row.get("idempotency_key")) if \
            hasattr(crm, "status_history_for_key") else None
        if hist is not None:
            row["crm_status_history"] = hist
        delivery_detail.append(row)

    outbox_entries = [e for e in outbox.read_all() if e.get("lead_id") == lead_id]

    ai_reason = lead.get("ai_reason") or ""
    return {
        "lead_id": lead_id,
        "tenant_id": repo.tenant_id,
        "stage": lead.get("stage"),
        "stage_is_delivery_ready": lead.get("stage") in (
            LeadStage.HUMAN_APPROVED.value,
            LeadStage.CRM_SYNCED.value,
            LeadStage.OUTBOXED.value,
        ),
        "canonical_key": lead.get("canonical_key"),
        "duplicate_of": lead.get("duplicate_of"),
        "identity": {
            "email": lead.get("email"),
            "phone": lead.get("phone"),
            "name": lead.get("name"),
            "company": lead.get("company"),
            "country": lead.get("country"),
            "budget_band": lead.get("budget_band"),
            "intent_level": lead.get("intent"),
        },
        "normalize_flags": {
            "opt_out": int(lead.get("opt_out") or 0),
            "injection": int(lead.get("injection_flag") or 0),
            "conflict": int(lead.get("conflict_flag") or 0),
            "needs_review": int(lead.get("needs_review") or 0),
        },
        "rules": {
            "verdict": lead.get("rules_verdict"),
            "reason": lead.get("rules_reason") or "",
        },
        "ai": {
            "verdict": lead.get("ai_verdict"),
            "confidence": lead.get("ai_confidence"),
            # ai_reason stores "; ".join(safety_flags) or "ok" (see orchestrator)
            "reason_or_safety_flags": ai_reason,
            "safety_flagged": bool(ai_reason) and ai_reason != "ok",
        },
        "combined_verdict": lead.get("combined_verdict"),
        "draft": None if draft is None else {
            "status": draft.get("status"),
            "subject": draft.get("subject"),
            "body": draft.get("body"),
            "evidence_field_ids": draft.get("evidence_field_ids"),
            "created_at": draft.get("created_at"),
        },
        "decisions": decisions,
        "delivery_ops": delivery_detail,
        "outbox_entries": outbox_entries,
        "events": events,
        "sources": sources,
    }
