"""Pipeline orchestrator (ties all stages together).

Single-record path: store source (evidence, idempotent) -> normalize -> dedup ->
qualify (rules + safe AI) -> build evidence-only draft -> set the pre-human
stage. Delivery is a SEPARATE, gated step that only runs after a human approves,
so an automated run can never push a message to CRM/outbox on its own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..db import LeadRepository
from ..models import LeadStage, Verdict, assert_transition
from . import dedup as dedup_mod
from . import normalize as nz
from .draft import Draft, build_draft
from .qualify_ai import AIResult, MockLLMProvider, combine_verdicts, qualify_ai
from .qualify_rules import qualify_rules

_VERDICT_TO_STAGE = {
    Verdict.APPROVE_READY.value: LeadStage.APPROVED_READY,
    Verdict.MANUAL_REVIEW.value: LeadStage.MANUAL_REVIEW,
    Verdict.REJECT.value: LeadStage.REJECTED,
}


def idem_key_for_raw(source_type: str, raw: dict, hint: str | None = None) -> str:
    """Deterministic inbound idempotency key: an explicit hint wins, otherwise a
    content hash so re-ingesting the same record is a no-op."""
    if hint:
        return f"{source_type}:{hint}"
    import json

    blob = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return f"{source_type}:hash:" + hashlib.sha256(blob.encode()).hexdigest()[:24]


@dataclass
class IngestResult:
    lead_id: str
    stage: str
    is_duplicate: bool
    newly_created_source: bool


@dataclass
class RunReport:
    processed: int = 0
    duplicates: int = 0
    dropped_duplicate_sources: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def bump(self, stage: str) -> None:
        self.by_stage[stage] = self.by_stage.get(stage, 0) + 1


class LeadPipeline:
    def __init__(self, repo: LeadRepository, provider: MockLLMProvider | None = None, *, ai_threshold: float = 0.75) -> None:
        self.repo = repo
        self.provider = provider or MockLLMProvider()
        self.ai_threshold = ai_threshold

    # -- ingestion (source + normalize + dedup) ----------------------------
    def ingest_raw(self, raw: dict, source_type: str, *, idempotency_hint: str | None = None) -> IngestResult | None:
        key = idem_key_for_raw(source_type, raw, idempotency_hint)
        source_row = self.repo.add_source(source_type, key, raw, lead_id=None)
        if source_row is None:
            # inbound idempotency: this exact payload was already ingested
            return None

        normalized = nz.normalize(raw)
        result = dedup_mod.dedupe_and_store(self.repo, normalized, source_row["id"])
        return IngestResult(
            lead_id=result.lead["id"],
            stage=result.lead["stage"],
            is_duplicate=result.is_duplicate,
            newly_created_source=True,
        )

    def ingest_many(self, records: list[dict], source_type: str, *, hints: list[str] | None = None) -> RunReport:
        report = RunReport()
        for i, raw in enumerate(records):
            hint = hints[i] if hints else None
            res = self.ingest_raw(raw, source_type, idempotency_hint=hint)
            if res is None:
                report.dropped_duplicate_sources += 1
                continue
            report.processed += 1
            if res.is_duplicate:
                report.duplicates += 1
        return report

    # -- qualification (rules + AI + draft) -------------------------------
    def qualify_lead(self, lead_id: str) -> tuple[RulesAndAI, Draft]:
        lead = self.repo.get_lead(lead_id)
        if lead is None:
            raise KeyError(lead_id)
        # rebuild the evidence view the AI/draft layers need
        enriched = self._enrich(lead)

        rules = qualify_rules(enriched)
        ai = qualify_ai(enriched, self.provider, ai_confidence_threshold=self.ai_threshold)
        combined = combine_verdicts(rules.verdict, ai)

        self.repo.update_lead_fields(lead_id, {
            "rules_verdict": rules.verdict,
            "rules_reason": "; ".join(rules.reasons),
            "ai_verdict": ai.verdict,
            "ai_confidence": ai.confidence,
            "ai_reason": "; ".join(ai.safety_flags) or "ok",
            "combined_verdict": combined,
        })

        # draft is built for every lead that is not a hard reject; for unsafe
        # leads it is a quarantined placeholder (see draft.build_draft).
        draft = build_draft(enriched)
        self.repo.add_draft(
            lead_id, "email", draft.subject, draft.body, draft.evidence_field_ids,
            status="pending_review",
        )

        stage = _VERDICT_TO_STAGE[combined]
        current = self.repo.get_lead(lead_id)
        assert current is not None
        if current["stage"] in (LeadStage.DEDUPED.value, LeadStage.RECEIVED.value, LeadStage.NORMALIZED.value):
            # normalized -> deduped -> qualify stage
            if current["stage"] != LeadStage.DEDUPED.value:
                self._advance_to_deduped(lead_id)
            assert_transition(LeadStage.DEDUPED.value, stage.value)
            self.repo.set_stage(lead_id, stage.value)
        return (RulesAndAI(rules, ai, combined)), draft

    def _advance_to_deduped(self, lead_id: str) -> None:
        lead = self.repo.get_lead(lead_id)
        assert lead is not None
        if lead["stage"] == LeadStage.RECEIVED.value:
            assert_transition(lead["stage"], LeadStage.NORMALIZED.value)
            self.repo.set_stage(lead_id, LeadStage.NORMALIZED.value)
            lead = self.repo.get_lead(lead_id)
        if lead and lead["stage"] == LeadStage.NORMALIZED.value:
            assert_transition(lead["stage"], LeadStage.DEDUPED.value)
            self.repo.set_stage(lead_id, LeadStage.DEDUPED.value)

    def _enrich(self, lead: dict) -> dict:
        """Recompute the derived evidence view from a stored lead row."""
        enriched = dict(lead)
        enriched["evidence"] = nz.build_evidence(lead)
        return enriched

    def lead_view(self, lead_id: str) -> dict | None:
        """Public, side-effect-free enriched view for offline analysis
        (calibration / what-if). Returns None if the lead is not in this tenant."""
        lead = self.repo.get_lead(lead_id)
        return None if lead is None else self._enrich(lead)

    # -- full automated run up to (not including) delivery ----------------
    def run_qualification(self) -> RunReport:
        """Qualify every lead that is still pre-qualification. Does NOT deliver."""
        report = RunReport()
        for lead in self.repo.list_leads():
            if lead["stage"] in (LeadStage.RECEIVED.value, LeadStage.NORMALIZED.value, LeadStage.DEDUPED.value):
                _, _ = self.qualify_lead(lead["id"])
                updated = self.repo.get_lead(lead["id"])
                report.bump(updated["stage"])  # type: ignore[index]
                report.processed += 1
        return report


class RulesAndAI:
    def __init__(self, rules, ai: AIResult, combined: str) -> None:
        self.rules = rules
        self.ai = ai
        self.combined = combined
