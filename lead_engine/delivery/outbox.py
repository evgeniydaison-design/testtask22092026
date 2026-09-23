"""Local mock-outbox (requirement: local only, never sends).

Append-only JSONL per tenant. Every write is guarded by the human approval gate
via ``assert_deliverable`` before it is passed a lead that must already be
human_approved - the outbox also refuses anything not explicitly approved, so
there are two independent checks that an unapproved draft cannot be enqueued.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..approval.human_gate import assert_deliverable
from ..db import LeadRepository


class OutboxForbidden(RuntimeError):
    pass


class MockOutbox:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _existing_ids(self) -> set[str]:
        out = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.add(json.loads(line).get("lead_id"))
                except json.JSONDecodeError:
                    continue
        return out

    def write(self, repo: LeadRepository, lead_id: str, *, channel: str = "email") -> dict:
        # hard guard #2: even if called directly, an unapproved lead cannot be
        # written to the outbox.
        lead = assert_deliverable(repo, lead_id)
        draft = repo.get_draft_for_lead(lead_id)
        if draft is None:
            raise OutboxForbidden(f"lead {lead_id} has no draft to enqueue")

        # idempotent: never enqueue the same lead twice
        already = self.read_all()
        if any(e["lead_id"] == lead_id for e in already):
            return next(e for e in already if e["lead_id"] == lead_id)

        entry = {
            "outbox_id": f"obx_{uuid.uuid4().hex[:12]}",
            "tenant_id": repo.tenant_id,
            "lead_id": lead_id,
            "channel": channel,
            "to": lead.get("email") or lead.get("phone"),
            "subject": draft["subject"],
            "body": draft["body"],
            "draft_status": draft["status"],
            "delivery": "MOCK_LOCAL_ONLY_NOT_SENT",
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def count(self) -> int:
        return len(self.read_all())
