"""Tamper-evident audit trail (defense-in-depth for the human-approval loop).

The whole product rests on one claim: *a message never reached the outbox
without a recorded human approval*. That claim is only as strong as the
durability of the audit rows behind it. If an operator could quietly edit a
``decisions`` row after the fact (or delete an inconvenient ``events`` row),
every downstream report would still look clean.

This module makes such edits *detectable* with a SHA-256 hash chain over the
tenant's append-only audit records, in the spirit of a transparency log:

  * ``build_chain`` folds every event + decision into a linked sequence where
    each link hashes the previous link's hash (a ``prev`` pointer). Order is
    fully deterministic (created_at, then kind, then id) so two honest runs
    produce byte-identical chains.
  * ``seal`` writes the running head hash + length to a file that lives
    *outside* the lead store, so it can be copied to independent WORM storage.
  * ``verify`` rebuilds the chain from the current rows and compares it to the
    seal. Any insert, delete or edit of an audited row changes the head and is
    reported as ``TAMPERED``.

This is deliberately about *detectability*, not secrecy: the seal file is not
encrypted and needs no key. In production you would publish the head hash to a
service the auditor controls; the property being demonstrated is identical.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import LeadRepository

GENESIS = "0" * 64
ALGO = "sha256-chain-v1"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _leaf_hash(kind: str, seq: int, prev: str, payload: dict) -> str:
    body = f"{seq}|{kind}|{prev}|" + _canonical(payload)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_chain(repo: LeadRepository) -> list[dict]:
    """Fold the tenant's events + decisions into a deterministic hash chain.

    Returns a list of links; each is ``{seq, kind, ref_id, at, prev, hash}``.
    The chain depends only on the audited row contents + order, never on wall
    clock, so ``verify`` on another machine reproduces the same head.
    """
    stream: list[tuple[str, str, str, dict]] = []
    for e in repo.list_events_for_chain():
        payload = {
            "id": e["id"], "lead_id": e["lead_id"], "stage": e["stage"],
            "detail": e["detail"], "created_at": e["created_at"],
        }
        # tie-break tag "0.." keeps events before decisions at equal ts+id
        stream.append((e["created_at"], f"0event:{e['id']}", "event", payload))
    for d in repo.list_decisions_for_chain():
        payload = {
            "id": d["id"], "lead_id": d["lead_id"], "action": d["action"],
            "actor": d["actor"], "note": d["note"], "created_at": d["created_at"],
        }
        stream.append((d["created_at"], f"1decision:{d['id']}", "decision", payload))

    stream.sort(key=lambda r: (r[0], r[1]))

    links: list[dict] = []
    prev = GENESIS
    for seq, (_at, _tie, kind, payload) in enumerate(stream):
        h = _leaf_hash(kind, seq, prev, payload)
        links.append({
            "seq": seq, "kind": kind, "ref_id": payload["id"],
            "at": payload["created_at"], "prev": prev, "hash": h,
        })
        prev = h
    return links


def head_of(links: list[dict]) -> str:
    return links[-1]["hash"] if links else GENESIS


def seal(repo: LeadRepository, seal_path: Path) -> dict:
    """Compute the current chain head and persist it as an external seal."""
    links = build_chain(repo)
    record = {
        "tenant_id": repo.tenant_id,
        "algo": ALGO,
        "count": len(links),
        "head": head_of(links),
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    seal_path = Path(seal_path)
    seal_path.parent.mkdir(parents=True, exist_ok=True)
    seal_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def verify(repo: LeadRepository, seal_path: Path) -> dict:
    """Rebuild the chain and compare it to the stored seal.

    status: ``verified`` | ``TAMPERED`` | ``no_seal``.
    """
    seal_path = Path(seal_path)
    links = build_chain(repo)
    current_head = head_of(links)
    if not seal_path.exists():
        return {
            "status": "no_seal", "tenant_id": repo.tenant_id, "sealed": False,
            "current_head": current_head, "current_count": len(links),
        }
    rec = json.loads(seal_path.read_text(encoding="utf-8"))
    ok = current_head == rec.get("head") and len(links) == rec.get("count")
    return {
        "status": "verified" if ok else "TAMPERED",
        "tenant_id": repo.tenant_id,
        "sealed": True,
        "algo": rec.get("algo", ALGO),
        "sealed_at": rec.get("sealed_at"),
        "sealed_head": rec.get("head"),
        "current_head": current_head,
        "sealed_count": rec.get("count"),
        "current_count": len(links),
        "head_matches": current_head == rec.get("head"),
        "count_matches": len(links) == rec.get("count"),
    }
