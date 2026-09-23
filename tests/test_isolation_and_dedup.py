"""Tenant isolation (requirement 1) + dedup / idempotency / evidence
preservation (requirement 2) at the storage + pipeline level."""

from __future__ import annotations

from lead_engine.pipeline import normalize as nz

from .conftest import ALPHA, BETA, STRONG


def _ingest(svc, raw, source="csv", hint=None):
    return svc.pipeline.ingest_raw(raw, source, idempotency_hint=hint)


def test_two_tenants_are_physically_separate_files(make, settings):
    a = make(ALPHA)
    b = make(BETA)
    # distinct lead store + crm files on disk
    assert a.repo.db_path != b.repo.db_path
    assert settings.lead_store_path(ALPHA).name == "tenant_alpha.lead_store.db"
    assert settings.crm_store_path(BETA).name == "tenant_beta.crm.db"


def test_lead_from_alpha_not_visible_in_beta(make):
    a = make(ALPHA)
    b = make(BETA)
    res = _ingest(a, STRONG)
    assert res is not None
    # Beta's scoped repository cannot see Alpha's lead, by id or by key
    assert b.repo.get_lead(res.lead_id) is None
    assert b.repo.get_lead_by_key(nz.normalize(STRONG)["canonical_key"]) is None
    assert b.repo.list_leads() == []


def test_beta_cannot_mutate_alpha_lead(make):
    a = make(ALPHA)
    b = make(BETA)
    res = _ingest(a, STRONG)
    # Even an explicit set_stage against Beta's repo touches zero Alpha rows
    b.repo.set_stage(res.lead_id, "rejected")
    assert a.repo.get_lead(res.lead_id)["stage"] != "rejected"


def test_cross_source_dedup_preserves_all_evidence(make):
    a = make(ALPHA)
    first = _ingest(a, STRONG, "csv")
    dup = dict(STRONG)
    dup["notes"] = "came from webhook"
    second = _ingest(a, dup, "webhook", hint="wh-1")

    assert first.lead_id == second.lead_id           # same canonical person
    assert second.is_duplicate is True               # flagged as duplicate
    lead = a.repo.get_lead(first.lead_id)
    assert lead["duplicate_of"] == lead["id"]        # absorbed a duplicate
    sources = a.repo.get_sources_for_lead(first.lead_id)
    assert len(sources) == 2                          # both original records kept
    assert {s["source_type"] for s in sources} == {"csv", "webhook"}


def test_inbound_idempotency_webhook_key(make):
    a = make(ALPHA)
    r1 = _ingest(a, STRONG, "webhook", hint="idem-1")
    r2 = _ingest(a, STRONG, "webhook", hint="idem-1")   # same key -> ignored
    assert r1 is not None
    assert r2 is None
    assert a.repo.count_sources() == 1


def test_inbound_idempotency_content_hash(make):
    a = make(ALPHA)
    _ingest(a, STRONG, "json")
    again = _ingest(a, STRONG, "json")   # identical payload, no explicit hint
    assert again is None
    assert a.repo.count_sources() == 1


def test_dedup_keeps_richest_record_and_sticky_flags(make):
    a = make(ALPHA)
    _ingest(a, {"email": "x@example.com", "name": "X"}, "csv")
    # second source adds phone + raises a safety flag
    _ingest(a, {"email": "x@example.com", "phone": "+15550009999", "opt_out": "true"}, "webhook", hint="w")
    lead = a.repo.get_lead_by_key("email:x@example.com")
    assert lead["phone"] == "+15550009999"     # merged richer field
    assert lead["opt_out"] == 1                 # sticky safety flag
