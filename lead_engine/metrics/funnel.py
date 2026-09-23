"""Funnel metrics (requirement 5).

Aggregates per-tenant counts across the pipeline so the whole flow is
observable: how many leads entered, how they were qualified, how many needed a
human, how many delivered, and how the resilient delivery layer behaved
(retries / 429 / 5xx / DLQ / reprocess).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..db import LeadRepository
from ..delivery.crm_mock import MockCRM
from ..delivery.outbox import MockOutbox
from ..models import DeliveryState, LeadStage


@dataclass
class FunnelMetrics:
    tenant_id: str
    sources: int = 0
    leads_total: int = 0
    duplicates_merged: int = 0
    stage_counts: dict[str, int] = field(default_factory=dict)
    verdicts: dict[str, int] = field(default_factory=dict)
    ai_safety_flagged: int = 0
    manual_review_rate: float = 0.0
    delivery: dict[str, int] = field(default_factory=dict)
    crm_status_histogram: dict[str, int] = field(default_factory=dict)
    outbox_entries: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def compute(repo: LeadRepository, crm: MockCRM, outbox: MockOutbox) -> FunnelMetrics:
    m = FunnelMetrics(tenant_id=repo.tenant_id)
    m.sources = repo.count_sources()
    leads = repo.list_leads()
    m.leads_total = len(leads)
    m.duplicates_merged = sum(1 for l in leads if l.get("duplicate_of"))
    m.stage_counts = {k: v for k, v in sorted(repo.stage_counts().items())}

    for l in leads:
        v = l.get("combined_verdict") or "unqualified"
        m.verdicts[v] = m.verdicts.get(v, 0) + 1
        if (l.get("ai_reason") or "") not in ("", "ok") and l.get("ai_verdict") == LeadStage.MANUAL_REVIEW.value:
            m.ai_safety_flagged += 1

    reviewable = m.leads_total - m.verdicts.get("reject", 0)
    manual = m.stage_counts.get(LeadStage.MANUAL_REVIEW.value, 0)
    m.manual_review_rate = round(manual / reviewable, 3) if reviewable else 0.0

    for op in repo.list_delivery_ops():
        m.delivery[op["state"]] = m.delivery.get(op["state"], 0) + 1
    # ensure all states present
    for st in DeliveryState:
        m.delivery.setdefault(st.value, 0)

    m.crm_status_histogram = {str(k): v for k, v in sorted(crm.status_histogram().items())}
    m.outbox_entries = outbox.count()
    return m
