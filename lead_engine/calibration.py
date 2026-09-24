"""Rules-vs-AI calibration and threshold what-if analysis.

The pipeline combines a deterministic rules verdict with a guarded AI verdict
(see :func:`pipeline.qualify_ai.combine_verdicts`). This module turns that
combination into something a reviewer / ML engineer can actually reason about:

  * ``disagreement_matrix`` - cross-tab of the persisted rules vs AI verdicts,
    plus the headline safety number: how many leads the *rules* alone would have
    auto-passed but the AI/grounding gate independently escalated to a human.
    That count is the entire justification for adding the model at all.

  * ``threshold_sweep`` - re-runs the (pure, side-effect-free) AI + combine logic
    over every qualified lead at a ladder of ``ai_confidence_threshold`` values
    and tallies the resulting verdict mix. It answers "what if we tuned the
    knob?" without ever writing to the store, and is provably monotonic: lowering
    the threshold can only move leads from manual_review toward approve_ready,
    never the reverse.

Nothing here mutates state; it reads the current lead rows and recomputes.
"""

from __future__ import annotations

from .db import LeadRepository
from .models import Verdict
from .pipeline.qualify_ai import combine_verdicts, qualify_ai

DEFAULT_THRESHOLDS = (0.0, 0.25, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0)

_VERDICT_KEYS = (
    Verdict.APPROVE_READY.value,
    Verdict.MANUAL_REVIEW.value,
    Verdict.REJECT.value,
)


def _qualified_leads(repo: LeadRepository) -> list[dict]:
    return [l for l in repo.list_leads() if l.get("rules_verdict")]


def disagreement_matrix(pipeline) -> dict:
    """Cross-tab stored rules_verdict vs ai_verdict for qualified leads.

    Both directions of disagreement are meaningful engineering signal:
      * ``ai_more_lenient`` - the AI would have auto-passed a lead the rules
        conservatively held back. The rules layer earns its keep here.
      * ``ai_more_strict`` - the AI (or its grounding/safety guards) escalated
        a lead the rules would have passed. The model + adapter layer earns
        its keep here. On the shipped fixtures this is 0 because the mock AI
        tracks the rules closely, but with a real provider this is the whole
        point of the second opinion; the code path is covered by
        :class:`pipeline.qualify_ai.AdversarialMockLLMProvider` in tests.
    """
    repo = pipeline.repo
    matrix: dict[str, dict[str, int]] = {r: {a: 0 for a in _VERDICT_KEYS} for r in _VERDICT_KEYS}
    safety_catches = 0    # rules approve_ready + final combined went to manual (AI gate)
    ai_more_lenient = 0   # AI wanted approve, rules held
    ai_more_strict = 0    # AI went stricter than rules
    agree = 0
    for l in _qualified_leads(repo):
        r = l.get("rules_verdict")
        a = l.get("ai_verdict") or ""
        c = l.get("combined_verdict") or ""
        if r in matrix and a in matrix[r]:
            matrix[r][a] += 1
        if r == Verdict.APPROVE_READY.value and c == Verdict.MANUAL_REVIEW.value:
            safety_catches += 1
        if r and a and _strictness(a) < _strictness(r):
            ai_more_lenient += 1
        if r and a and _strictness(a) > _strictness(r):
            ai_more_strict += 1
        if a == r:
            agree += 1
    total = sum(sum(row.values()) for row in matrix.values())
    return {
        "matrix": matrix,
        "total": total,
        "agree": agree,
        "disagree": total - agree,
        "ai_more_lenient": ai_more_lenient,
        "ai_more_strict": ai_more_strict,
        "safety_catches": safety_catches,
    }


def _strictness(verdict: str) -> int:
    return {"approve_ready": 0, "manual_review": 1, "reject": 2}.get(verdict, 0)


def threshold_sweep(pipeline, thresholds=DEFAULT_THRESHOLDS) -> dict:
    """Recompute the combined verdict at each threshold; tally the mix."""
    repo = pipeline.repo
    leads = _qualified_leads(repo)
    rows: list[dict] = []
    for t in thresholds:
        mix = {k: 0 for k in _VERDICT_KEYS}
        for l in leads:
            enriched = pipeline._enrich(l)
            ai = qualify_ai(enriched, pipeline.provider, ai_confidence_threshold=t)
            combined = combine_verdicts(l["rules_verdict"], ai)
            mix[combined] = mix.get(combined, 0) + 1
        rows.append({"threshold": round(float(t), 3), **mix,
                     "auto_rate": _rate(mix["approve_ready"], len(leads))})
    # monotonicity proof: approve_ready is non-increasing as threshold rises
    approvals = [r["approve_ready"] for r in sorted(rows, key=lambda x: x["threshold"])]
    monotonic = all(approvals[i] >= approvals[i + 1] for i in range(len(approvals) - 1))
    return {
        "current_threshold": pipeline.ai_threshold,
        "leads_evaluated": len(leads),
        "sweep": rows,
        "monotonic": monotonic,
    }


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def compute(pipeline) -> dict:
    return {
        "tenant_id": pipeline.repo.tenant_id,
        "disagreement": disagreement_matrix(pipeline),
        "calibration": threshold_sweep(pipeline),
    }
