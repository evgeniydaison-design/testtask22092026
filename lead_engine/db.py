"""Per-tenant SQLite persistence with a strictly scoped repository.

Isolation model (requirement 1):
  * Each tenant gets its OWN lead store file ``data/<tenant>.lead_store.db``.
    A repository instance is bound to one tenant and can physically only see
    that tenant's file - there is no cross-tenant query path at all.
  * ``tenant_id`` is still written on every row as defense-in-depth and every
    query filters on it, so even a mis-pointed connection stays scoped.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    name TEXT,
    company TEXT,
    country TEXT,
    budget_band TEXT,
    intent TEXT,
    opt_out INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    injection_flag INTEGER NOT NULL DEFAULT 0,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL,
    rules_verdict TEXT,
    rules_reason TEXT,
    ai_verdict TEXT,
    ai_confidence REAL,
    ai_reason TEXT,
    combined_verdict TEXT,
    duplicate_of TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS lead_sources (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_id TEXT,
    source_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    evidence_field_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_ops (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    op_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS dlq (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    lead_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    error_profile TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    lead_id TEXT,
    stage TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads (tenant_id, stage);
CREATE INDEX IF NOT EXISTS idx_sources_lead ON lead_sources (tenant_id, lead_id);
CREATE INDEX IF NOT EXISTS idx_ops_state ON delivery_ops (tenant_id, state);
"""

_NOW = "CURRENT_TIMESTAMP"


def _utcnow() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class LeadRepository:
    """A repository bound to a single tenant. All reads/writes are scoped."""

    def __init__(self, tenant_id: str, db_path: Path) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.tenant_id = tenant_id
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LeadRepository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internal -----------------------------------------------------------
    def _exec(self, sql: str, params: Iterable[Any] = ()):  # noqa: ANN202
        return self._conn.execute(sql, tuple(params))

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self._exec(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # -- leads --------------------------------------------------------------
    def upsert_lead(self, lead: dict) -> dict:
        """Insert or update a lead within this tenant (upsert on canonical_key)."""
        now = _utcnow()
        existing = self.get_lead_by_key(lead["canonical_key"])
        if existing:
            lead_id = existing["id"]
            fields = {k: v for k, v in lead.items() if k not in ("id", "tenant_id", "canonical_key")}
            fields["updated_at"] = now
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._exec(
                f"UPDATE leads SET {cols} WHERE tenant_id = ? AND id = ?",
                (*fields.values(), self.tenant_id, lead_id),
            )
        else:
            lead_id = lead.get("id") or _new_id("lead")
            record = {
                "id": lead_id,
                "tenant_id": self.tenant_id,
                "created_at": lead.get("created_at", now),
                "updated_at": now,
                **{k: v for k, v in lead.items() if k not in ("id", "tenant_id", "created_at")},
            }
            # ensure tenant not overridden by caller payload
            record["tenant_id"] = self.tenant_id
            cols = ", ".join(record.keys())
            ph = ", ".join("?" for _ in record)
            self._exec(f"INSERT INTO leads ({cols}) VALUES ({ph})", list(record.values()))
        self._conn.commit()
        out = self.get_lead(lead_id)
        assert out is not None
        return out

    def get_lead(self, lead_id: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM leads WHERE tenant_id = ? AND id = ?",
            (self.tenant_id, lead_id),
        )
        return rows[0] if rows else None

    def get_lead_by_key(self, canonical_key: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM leads WHERE tenant_id = ? AND canonical_key = ?",
            (self.tenant_id, canonical_key),
        )
        return rows[0] if rows else None

    def list_leads(self, stage: str | None = None) -> list[dict]:
        if stage:
            rows = self._all(
                "SELECT * FROM leads WHERE tenant_id = ? AND stage = ? ORDER BY created_at",
                (self.tenant_id, stage),
            )
        else:
            rows = self._all(
                "SELECT * FROM leads WHERE tenant_id = ? ORDER BY created_at",
                (self.tenant_id,),
            )
        return rows

    def set_stage(self, lead_id: str, stage: str) -> None:
        self._exec(
            "UPDATE leads SET stage = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
            (stage, _utcnow(), self.tenant_id, lead_id),
        )
        self._conn.commit()
        self.add_event(lead_id, stage)

    def update_lead_fields(self, lead_id: str, fields: dict) -> None:
        if not fields:
            return
        fields = {k: v for k, v in fields.items() if k not in ("id", "tenant_id")}
        fields["updated_at"] = _utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(
            f"UPDATE leads SET {cols} WHERE tenant_id = ? AND id = ?",
            (*fields.values(), self.tenant_id, lead_id),
        )
        self._conn.commit()

    def stage_counts(self) -> dict[str, int]:
        rows = self._all(
            "SELECT stage, COUNT(*) AS c FROM leads WHERE tenant_id = ? GROUP BY stage",
            (self.tenant_id,),
        )
        return {r["stage"]: r["c"] for r in rows}

    # -- sources / evidence -------------------------------------------------
    def add_source(self, source_type: str, idempotency_key: str, raw: dict, lead_id: str | None = None) -> dict | None:
        """Insert a source snapshot. Returns the row, or None if the idempotency
        key already existed (inbound idempotency, requirement 2)."""
        sid = _new_id("src")
        try:
            self._exec(
                "INSERT INTO lead_sources (id, tenant_id, lead_id, source_type, idempotency_key, raw_json, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, self.tenant_id, lead_id, source_type, idempotency_key, json.dumps(raw, ensure_ascii=False, sort_keys=True), _utcnow()),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # duplicate idempotency_key -> no new source created
            return None
        rows = self._all(
            "SELECT * FROM lead_sources WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, idempotency_key),
        )
        return rows[0] if rows else None

    def link_source_to_lead(self, source_id: str, lead_id: str) -> None:
        self._exec(
            "UPDATE lead_sources SET lead_id = ? WHERE tenant_id = ? AND id = ?",
            (lead_id, self.tenant_id, source_id),
        )
        self._conn.commit()

    def get_source_by_key(self, idempotency_key: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM lead_sources WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, idempotency_key),
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["raw"] = json.loads(r.pop("raw_json"))
        return r

    def get_sources_for_lead(self, lead_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM lead_sources WHERE tenant_id = ? AND lead_id = ? ORDER BY collected_at",
            (self.tenant_id, lead_id),
        )
        out = []
        for r in rows:
            r = dict(r)
            r["raw"] = json.loads(r.pop("raw_json"))
            out.append(r)
        return out

    def count_sources(self) -> int:
        rows = self._all("SELECT COUNT(*) AS c FROM lead_sources WHERE tenant_id = ?", (self.tenant_id,))
        return rows[0]["c"]

    # -- drafts -------------------------------------------------------------
    def add_draft(self, lead_id: str, channel: str, subject: str, body: str, evidence_field_ids: list[str], status: str = "pending_review") -> dict:
        did = _new_id("draft")
        self._exec(
            "INSERT INTO drafts (id, tenant_id, lead_id, channel, subject, body, evidence_field_ids, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, self.tenant_id, lead_id, channel, subject, body, json.dumps(evidence_field_ids), status, _utcnow()),
        )
        self._conn.commit()
        return self.get_draft_for_lead(lead_id)  # type: ignore[return-value]

    def get_draft_for_lead(self, lead_id: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM drafts WHERE tenant_id = ? AND lead_id = ? ORDER BY created_at DESC LIMIT 1",
            (self.tenant_id, lead_id),
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["evidence_field_ids"] = json.loads(r["evidence_field_ids"])
        return r

    def set_draft_status(self, draft_id: str, status: str, new_body: str | None = None) -> None:
        if new_body is not None:
            self._exec(
                "UPDATE drafts SET body = ?, status = ? WHERE tenant_id = ? AND id = ?",
                (new_body, status, self.tenant_id, draft_id),
            )
        else:
            self._exec(
                "UPDATE drafts SET status = ? WHERE tenant_id = ? AND id = ?",
                (status, self.tenant_id, draft_id),
            )
        self._conn.commit()

    # -- decisions ----------------------------------------------------------
    def add_decision(self, lead_id: str, action: str, actor: str, note: str = "") -> dict:
        did = _new_id("dec")
        self._exec(
            "INSERT INTO decisions (id, tenant_id, lead_id, action, actor, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (did, self.tenant_id, lead_id, action, actor, note, _utcnow()),
        )
        self._conn.commit()
        return {"id": did, "lead_id": lead_id, "action": action, "actor": actor}

    def list_decisions(self, lead_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM decisions WHERE tenant_id = ? AND lead_id = ? ORDER BY created_at",
            (self.tenant_id, lead_id),
        )

    def has_approved_decision(self, lead_id: str) -> bool:
        rows = self._all(
            "SELECT COUNT(*) AS c FROM decisions WHERE tenant_id = ? AND lead_id = ? AND action = 'approve'",
            (self.tenant_id, lead_id),
        )
        return rows[0]["c"] > 0

    # -- delivery ops -------------------------------------------------------
    def create_delivery_op(self, lead_id: str, op_type: str, idempotency_key: str, max_attempts: int = 5) -> dict:
        existing = self.get_delivery_op_by_key(idempotency_key)
        if existing:
            return existing
        oid = _new_id("op")
        now = _utcnow()
        self._exec(
            "INSERT INTO delivery_ops (id, tenant_id, lead_id, op_type, idempotency_key, state, attempts, max_attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (oid, self.tenant_id, lead_id, op_type, idempotency_key, max_attempts, now, now),
        )
        self._conn.commit()
        op = self.get_delivery_op(oid)
        assert op is not None
        return op

    def get_delivery_op(self, op_id: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM delivery_ops WHERE tenant_id = ? AND id = ?",
            (self.tenant_id, op_id),
        )
        return rows[0] if rows else None

    def get_delivery_op_by_key(self, idempotency_key: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM delivery_ops WHERE tenant_id = ? AND idempotency_key = ?",
            (self.tenant_id, idempotency_key),
        )
        return rows[0] if rows else None

    def update_delivery_op(self, op_id: str, fields: dict) -> None:
        fields = {k: v for k, v in fields.items() if k not in ("id", "tenant_id")}
        fields["updated_at"] = _utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(
            f"UPDATE delivery_ops SET {cols} WHERE tenant_id = ? AND id = ?",
            (*fields.values(), self.tenant_id, op_id),
        )
        self._conn.commit()

    def list_delivery_ops(self, state: str | None = None) -> list[dict]:
        if state:
            return self._all(
                "SELECT * FROM delivery_ops WHERE tenant_id = ? AND state = ? ORDER BY created_at",
                (self.tenant_id, state),
            )
        return self._all(
            "SELECT * FROM delivery_ops WHERE tenant_id = ? ORDER BY created_at",
            (self.tenant_id,),
        )

    # -- DLQ ----------------------------------------------------------------
    def add_dlq(self, op_id: str, lead_id: str, reason: str, error_profile: str | None = None) -> dict:
        did = _new_id("dlq")
        self._exec(
            "INSERT INTO dlq (id, tenant_id, op_id, lead_id, reason, error_profile, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (did, self.tenant_id, op_id, lead_id, reason, error_profile, _utcnow()),
        )
        self._conn.commit()
        return {"id": did, "op_id": op_id, "lead_id": lead_id, "reason": reason}

    def list_dlq(self, status: str | None = "open") -> list[dict]:
        if status:
            return self._all(
                "SELECT * FROM dlq WHERE tenant_id = ? AND status = ? ORDER BY created_at",
                (self.tenant_id, status),
            )
        return self._all("SELECT * FROM dlq WHERE tenant_id = ? ORDER BY created_at", (self.tenant_id,))

    def resolve_dlq(self, op_id: str) -> None:
        self._exec(
            "UPDATE dlq SET status = 'resolved' WHERE tenant_id = ? AND op_id = ? AND status = 'open'",
            (self.tenant_id, op_id),
        )
        self._conn.commit()

    # -- events -------------------------------------------------------------
    def add_event(self, lead_id: str | None, stage: str, detail: str | None = None) -> None:
        self._exec(
            "INSERT INTO events (id, tenant_id, lead_id, stage, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_new_id("evt"), self.tenant_id, lead_id, stage, detail, _utcnow()),
        )
        self._conn.commit()

    def count_events(self, stage: str | None = None) -> int:
        if stage:
            rows = self._all(
                "SELECT COUNT(*) AS c FROM events WHERE tenant_id = ? AND stage = ?",
                (self.tenant_id, stage),
            )
        else:
            rows = self._all("SELECT COUNT(*) AS c FROM events WHERE tenant_id = ?", (self.tenant_id,))
        return rows[0]["c"]
