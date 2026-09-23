"""Deduplication (requirement 2).

Given a normalized lead and the tenant-scoped repository, decide whether this
is a brand-new lead or a duplicate of an existing one. Duplicates never create
a new lead row: the new source is attached to the existing lead as evidence
and the lead is marked ``duplicate_of``. All original sources are preserved.
"""

from __future__ import annotations

from ..db import LeadRepository
from ..models import LeadStage


class DedupResult:
    def __init__(self, lead: dict, is_duplicate: bool, source_row: dict | None) -> None:
        self.lead = lead
        self.is_duplicate = is_duplicate
        self.source_row = source_row


def dedupe_and_store(repo: LeadRepository, normalized: dict, source_row_id: str | None) -> DedupResult:
    """Persist a normalized lead with dedup. ``normalized`` must contain a
    ``canonical_key``. Returns the effective lead row + duplicate flag."""
    key = normalized["canonical_key"]
    existing = repo.get_lead_by_key(key)

    # fields we persist for a lead (strip derived helper keys)
    persist = {k: v for k, v in normalized.items() if k not in ("evidence", "missing_fields")}

    if existing:
        lead_id = existing["id"]
        # link the new source to the existing lead (evidence preservation)
        if source_row_id:
            repo.link_source_to_lead(source_row_id, lead_id)
        # keep the richest record: fill empty fields, never overwrite with None
        merged = dict(existing)
        for f in ("email", "phone", "name", "company", "country", "budget_band", "intent"):
            if not merged.get(f) and persist.get(f):
                merged[f] = persist[f]
        # safety flags are sticky: once set, they stay set
        for flag in ("opt_out", "injection_flag", "conflict_flag", "needs_review"):
            merged[flag] = int(merged.get(flag, 0)) or int(persist.get(flag, 0))
        # mark that this lead absorbed a duplicate source (points at itself)
        merged["duplicate_of"] = lead_id
        merged["id"] = lead_id
        repo.upsert_lead(merged)
        return DedupResult(repo.get_lead(lead_id), True, None)

    persist["stage"] = LeadStage.RECEIVED.value
    lead = repo.upsert_lead(persist)
    if source_row_id:
        repo.link_source_to_lead(source_row_id, lead["id"])
    return DedupResult(lead, False, None)
