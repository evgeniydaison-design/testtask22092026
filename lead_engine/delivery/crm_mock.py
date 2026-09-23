"""Mock CRM with deterministic fault injection (requirement 5, deep module).

Simulates an external CRM over a small interface that returns HTTP-like status
codes so the retry layer can distinguish retryable (429 / 5xx / timeout) from
non-retryable (4xx) failures. Two idempotency mechanisms prevent duplicates:
  * per-operation idempotency key - a replayed call is a no-op and returns the
    original result (``replayed=True``)
  * contact upsert keyed by email - re-syncing the same lead never creates a
    second contact
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

RETRYABLE = {429, 500, 502, 503, 504}
NON_RETRYABLE = {400, 401, 403, 404, 405, 422}

_CRM_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    name TEXT,
    company TEXT,
    country TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    due TEXT,
    subject TEXT,
    body TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT,
    status INTEGER,
    created_at TEXT NOT NULL
);
"""


@dataclass
class CrmResponse:
    status: int
    body: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class MockCRM:
    def __init__(self, db_path: Path, *, error_profile: str = "none", flaky_keys: set[str] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.error_profile = error_profile
        # keys that fail exactly once (then succeed) - for flaky demos
        self._flaky_remaining: dict[str, int] = {}
        for k in (flaky_keys or set()):
            self._flaky_remaining[k] = 1
        self._call_count: dict[str, int] = {}
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CRM_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- fault injection ----------------------------------------------------
    def _maybe_fail(self, key: str) -> CrmResponse | None:
        profile = self.error_profile
        self._call_count[key] = self._call_count.get(key, 0) + 1
        n = self._call_count[key]
        if profile == "none":
            return None
        if profile == "flaky_429":
            # first two calls 429, then success
            return CrmResponse(429, {"error": "rate limited"}) if n <= 2 else None
        if profile == "flaky_5xx":
            return CrmResponse(503, {"error": "temporarily unavailable"}) if n <= 1 else None
        if profile == "always_500":
            return CrmResponse(500, {"error": "internal"})
        if profile == "non_retryable_405":
            return CrmResponse(405, {"error": "method not allowed"})
        return None

    # -- API ----------------------------------------------------------------
    def upsert_contact_and_task(self, *, idempotency_key: str, contact: dict, task: dict) -> CrmResponse:
        # idempotent replay short-circuit
        existing = self._idempotent_get(idempotency_key)
        if existing is not None:
            self._log(idempotency_key, existing["status"])
            return CrmResponse(existing["status"], {**existing["body"], "replayed": True})

        fail = self._maybe_fail(idempotency_key)
        if fail is not None:
            self._log(idempotency_key, fail.status)
            return fail

        cur = self._conn.cursor()
        email = (contact.get("email") or "").lower()
        contact_id = contact.get("id")
        row = cur.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
        if row:
            contact_id = row["id"]
        else:
            contact_id = contact_id or f"crm_{uuid.uuid4().hex[:12]}"
            cur.execute(
                "INSERT INTO contacts (id, email, name, company, country, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (contact_id, email, contact.get("name"), contact.get("company"), contact.get("country"), _now()),
            )
        # one open task per contact + key
        cur.execute(
            "INSERT INTO tasks (id, contact_id, owner, due, subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"task_{uuid.uuid4().hex[:12]}", contact_id, task.get("owner", "unassigned"),
             task.get("due"), task.get("subject"), task.get("body"), _now()),
        )
        result = CrmResponse(200, {"contact_id": contact_id, "email": email, "created": True})
        self._idempotent_put(idempotency_key, result)
        self._conn.commit()
        self._log(idempotency_key, 200)
        return result

    # -- helpers ------------------------------------------------------------
    def _idempotent_get(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT result_json FROM idempotency WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        import json

        return json.loads(row["result_json"])

    def _idempotent_put(self, key: str, res: CrmResponse) -> None:
        import json

        self._conn.execute(
            "INSERT OR REPLACE INTO idempotency (key, result_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps({"status": res.status, "body": res.body}), _now()),
        )

    def _log(self, key: str, status: int) -> None:
        self._conn.execute(
            "INSERT INTO calls (key, status, created_at) VALUES (?, ?, ?)", (key, status, _now())
        )
        self._conn.commit()

    # -- inspection ---------------------------------------------------------
    def count_contacts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    def count_tasks(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def get_contact_by_email(self, email: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM contacts WHERE email = ?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def status_histogram(self) -> dict[int, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) c FROM calls GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}
