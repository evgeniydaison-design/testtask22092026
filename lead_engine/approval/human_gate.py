"""Human approval gate (requirement 4) - the single hard choke point.

Nothing can move to CRM/outbox unless a human records an ``approve`` decision
here. ``assert_deliverable`` is called by the delivery service and the outbox
writer; it raises unless the lead is BOTH in stage ``human_approved`` AND has a
recorded approve decision. Two independent guards on purpose.
"""

from __future__ import annotations

from ..db import LeadRepository
from ..models import LeadStage, assert_transition


class ApprovalError(RuntimeError):
    pass


def review_view(repo: LeadRepository, lead_id: str) -> dict:
    """Everything an operator needs to decide: evidence + rationale + AI + draft."""
    lead = repo.get_lead(lead_id)
    if lead is None:
        raise ApprovalError(f"lead {lead_id} not found in tenant {repo.tenant_id}")
    return {
        "lead": lead,
        "sources": repo.get_sources_for_lead(lead_id),
        "draft": repo.get_draft_for_lead(lead_id),
        "decisions": repo.list_decisions(lead_id),
    }


def pending_queue(repo: LeadRepository) -> list[dict]:
    """Leads awaiting a human: manual_review plus auto-passed approved_ready
    (approved_ready still needs a click to confirm; it is never auto-delivered)."""
    out: list[dict] = []
    for stage in (LeadStage.MANUAL_REVIEW.value, LeadStage.APPROVED_READY.value):
        for lead in repo.list_leads(stage=stage):
            out.append({
                "lead_id": lead["id"],
                "stage": lead["stage"],
                "combined_verdict": lead.get("combined_verdict"),
                "company": lead.get("company"),
                "email": lead.get("email"),
                "flags": {
                    "opt_out": lead.get("opt_out"),
                    "injection": lead.get("injection_flag"),
                    "conflict": lead.get("conflict_flag"),
                },
            })
    return out


def approve_lead(repo: LeadRepository, lead_id: str, actor: str, note: str = "") -> dict:
    lead = repo.get_lead(lead_id)
    if lead is None:
        raise ApprovalError(f"lead {lead_id} not found")
    if int(lead.get("opt_out", 0)):
        # hard stop: an opt-out lead cannot be approved for contact
        raise ApprovalError("opt-out leads cannot be approved")
    frm = LeadStage(lead["stage"])
    # approved_ready -> human_approved and manual_review -> ... need manual first
    if frm == LeadStage.APPROVED_READY:
        assert_transition(frm, LeadStage.HUMAN_APPROVED)
    elif frm == LeadStage.MANUAL_REVIEW:
        assert_transition(frm, LeadStage.HUMAN_APPROVED)
    elif frm == LeadStage.HUMAN_APPROVED:
        raise ApprovalError("already approved")
    else:
        raise ApprovalError(f"cannot approve a lead in stage {lead['stage']}")

    repo.add_decision(lead_id, "approve", actor, note)
    repo.set_stage(lead_id, LeadStage.HUMAN_APPROVED.value)
    draft = repo.get_draft_for_lead(lead_id)
    if draft:
        repo.set_draft_status(draft["id"], "approved")
    return repo.get_lead(lead_id)


def reject_lead(repo: LeadRepository, lead_id: str, actor: str, note: str = "") -> dict:
    lead = repo.get_lead(lead_id)
    if lead is None:
        raise ApprovalError(f"lead {lead_id} not found")
    frm = LeadStage(lead["stage"])
    if frm in (LeadStage.MANUAL_REVIEW, LeadStage.APPROVED_READY):
        # approved_ready can be sent back to manual, then rejected; shortcut:
        if frm == LeadStage.APPROVED_READY:
            repo.set_stage(lead_id, LeadStage.MANUAL_REVIEW.value)
        assert_transition(LeadStage.MANUAL_REVIEW, LeadStage.HUMAN_REJECTED)
    else:
        raise ApprovalError(f"cannot reject a lead in stage {lead['stage']}")
    repo.add_decision(lead_id, "reject", actor, note)
    repo.set_stage(lead_id, LeadStage.HUMAN_REJECTED.value)
    draft = repo.get_draft_for_lead(lead_id)
    if draft:
        repo.set_draft_status(draft["id"], "rejected")
    return repo.get_lead(lead_id)


def edit_and_approve(repo: LeadRepository, lead_id: str, actor: str, new_body: str, note: str = "") -> dict:
    """Operator edits the draft text then approves. Editing does not bypass the
    gate - approve still records the decision and flips the stage."""
    draft = repo.get_draft_for_lead(lead_id)
    if draft is None:
        raise ApprovalError("no draft to edit")
    repo.set_draft_status(draft["id"], "pending_review", new_body=new_body)
    return approve_lead(repo, lead_id, actor, note=note or "approved after edit")


def assert_deliverable(repo: LeadRepository, lead_id: str) -> dict:
    """Guard used by delivery + outbox. Raises unless delivery is authorized."""
    lead = repo.get_lead(lead_id)
    if lead is None:
        raise ApprovalError(f"lead {lead_id} not found")
    if lead["stage"] != LeadStage.HUMAN_APPROVED.value:
        raise ApprovalError(
            f"lead {lead_id} not human_approved (stage={lead['stage']}); delivery forbidden"
        )
    if not repo.has_approved_decision(lead_id):
        raise ApprovalError(f"lead {lead_id} lacks an approve decision; delivery forbidden")
    if int(lead.get("opt_out", 0)):
        raise ApprovalError(f"lead {lead_id} is opt-out; delivery forbidden")
    return lead
