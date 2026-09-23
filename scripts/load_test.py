"""Synthetic load test - measures how the pipeline scales past the demo size.

Not a perf-tuning harness; just a smoke check that the architecture isn't
welded to a toy dataset. Runs N synthetic leads through the full pipeline
(ingest -> normalize -> dedup -> qualify) and reports wall time + throughput.

Usage:
    python scripts/load_test.py --n 1000
    python scripts/load_test.py --n 5000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ensure the repo root is importable when run as `python scripts/load_test.py`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lead_engine.config import Settings  # noqa: E402
from lead_engine.service import build_service  # noqa: E402


def make_batch(n: int, tenant: str = "tenant_alpha") -> list[dict]:
    """Deterministic synthetic records; ~15% collide by email to exercise dedup."""
    out: list[dict] = []
    for i in range(n):
        j = i % 512 if i % 7 == 0 else i  # ~14% cross-collisions
        out.append({
            "email": f"lt-{j}@example.com",
            "phone": f"+1555{1_000_000 + (j % 9_000_000):07d}",
            "name": f"Person {j}",
            "company": f"Company {j % 97}",
            "country": "US",
            "budget_band": "high" if i % 3 else "mid",
            "intent": "send a quote please",
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=1000, help="number of raw records to ingest")
    p.add_argument("--tenant", default="tenant_alpha")
    p.add_argument("--data-dir", default=None,
                   help="temp dir for the SQLite store (default: auto-clean tmp)")
    args = p.parse_args()

    import shutil, tempfile
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="athenai_lt_"))
    settings = Settings(data_dir=data_dir)

    t0 = time.perf_counter()
    svc = build_service(args.tenant, settings=settings)
    records = make_batch(args.n, args.tenant)
    t_prep = time.perf_counter() - t0

    t1 = time.perf_counter()
    ingest_report = svc.pipeline.ingest_many(records, "csv")
    t_ingest = time.perf_counter() - t1

    t2 = time.perf_counter()
    qual_report = svc.pipeline.run_qualification()
    t_qual = time.perf_counter() - t2

    t3 = time.perf_counter()
    total_leads = len(svc.repo.list_leads())
    stage_counts = svc.repo.stage_counts()
    t_read = time.perf_counter() - t3

    svc.close()
    if args.data_dir is None:
        shutil.rmtree(data_dir, ignore_errors=True)

    print(json.dumps({
        "n_records": args.n,
        "ingest_seconds": round(t_ingest, 3),
        "qualify_seconds": round(t_qual, 3),
        "prep_seconds": round(t_prep, 3),
        "read_seconds": round(t_read, 3),
        "records_per_second": round(args.n / max(t_ingest + t_qual, 1e-6), 1),
        "unique_leads": total_leads,
        "duplicates_merged": ingest_report.duplicates,
        "stage_counts": stage_counts,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
