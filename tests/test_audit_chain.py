"""Tamper-evident audit chain: honest verify passes, any edit/delete is caught."""

from __future__ import annotations

from lead_engine import audit_chain
from lead_engine.approval import human_gate

from .conftest import STRONG


def _qualified_approved(make):
    svc = make()
    res = svc.pipeline.ingest_raw(STRONG, "csv")
    svc.pipeline.qualify_lead(res.lead_id)
    human_gate.approve_lead(svc.repo, res.lead_id, actor="ops-alpha", note="ok")
    return svc, res.lead_id


def test_chain_is_deterministic(make):
    svc, _ = _qualified_approved(make)
    a = audit_chain.build_chain(svc.repo)
    b = audit_chain.build_chain(svc.repo)
    assert [l["hash"] for l in a] == [l["hash"] for l in b]
    assert audit_chain.head_of(a) == audit_chain.head_of(b)
    # a real chain links each entry to the previous head
    assert all(a[i]["prev"] == a[i - 1]["hash"] for i in range(1, len(a)))


def test_seal_then_verify_ok(make):
    svc, _ = _qualified_approved(make)
    seal_path = svc.settings.audit_seal_path(svc.tenant_id)
    audit_chain.seal(svc.repo, seal_path)
    res = audit_chain.verify(svc.repo, seal_path)
    assert res["status"] == "verified"
    assert res["head_matches"] and res["count_matches"]


def test_edit_decision_is_tampered(make):
    svc, lead_id = _qualified_approved(make)
    seal_path = svc.settings.audit_seal_path(svc.tenant_id)
    audit_chain.seal(svc.repo, seal_path)
    # a malicious operator rewrites who approved the lead
    dec = svc.repo.list_decisions(lead_id)[0]
    svc.repo._exec("UPDATE decisions SET actor = ? WHERE id = ?", ("mallory", dec["id"]))
    svc.repo._conn.commit()
    res = audit_chain.verify(svc.repo, seal_path)
    assert res["status"] == "TAMPERED"
    assert not res["head_matches"]


def test_delete_event_is_tampered(make):
    svc, lead_id = _qualified_approved(make)
    seal_path = svc.settings.audit_seal_path(svc.tenant_id)
    audit_chain.seal(svc.repo, seal_path)
    # delete an inconvenient stage-transition row
    ev = svc.repo.list_events_for_chain()[-1]
    svc.repo._exec("DELETE FROM events WHERE id = ?", (ev["id"],))
    svc.repo._conn.commit()
    res = audit_chain.verify(svc.repo, seal_path)
    assert res["status"] == "TAMPERED"
    assert not res["count_matches"]


def test_verify_without_seal(make):
    svc, _ = _qualified_approved(make)
    res = audit_chain.verify(svc.repo, svc.settings.audit_seal_path(svc.tenant_id))
    assert res["status"] == "no_seal"
    assert res["sealed"] is False
