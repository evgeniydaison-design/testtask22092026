"""Ingestion readers (requirement: multiple sources -> one normalized record).

All inputs are SYNTHETIC fixtures. Readers only return raw dicts; they never
send anything anywhere. ``read_csv``/``read_json`` are used by the CLI, and
``record_from_webhook`` by the mock webhook endpoint.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


def read_csv(path: str | Path) -> list[dict]:
    p = Path(path)
    with p.open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path: str | Path) -> list[dict]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # allow {"records": [...]} or a single object
        data = data.get("records", [data])
    if not isinstance(data, list):
        raise ValueError(f"{p.name}: expected a JSON array or an object with 'records'")
    return [dict(r) for r in data]


def record_from_webhook(payload: dict) -> dict:
    """Extract the raw lead record from a webhook envelope."""
    if "record" in payload and isinstance(payload["record"], dict):
        return dict(payload["record"])
    # tolerate a bare record body
    return {k: v for k, v in payload.items() if k not in ("tenant_id", "idempotency_key")}


READERS = {"csv": read_csv, "json": read_json}


def read_source(path: str | Path, fmt: str) -> list[dict]:
    fmt = fmt.lower().lstrip(".")
    if fmt not in READERS:
        raise ValueError(f"Unsupported source format {fmt!r}. Use one of {sorted(READERS)}")
    return READERS[fmt](path)
