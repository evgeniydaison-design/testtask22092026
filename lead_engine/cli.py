"""Command line interface for the AthenAI Lead Engine.

Everything runs locally on synthetic data. Typical operator flow:

    python -m lead_engine.cli ingest  --tenant tenant_alpha --format csv --path fixtures/leads_alpha.csv
    python -m lead_engine.cli qualify --tenant tenant_alpha
    python -m lead_engine.cli queue    --tenant tenant_alpha
    python -m lead_engine.cli review   --tenant tenant_alpha --lead <id>
    python -m lead_engine.cli approve  --tenant tenant_alpha --lead <id> --actor ops-alpha
    python -m lead_engine.cli deliver  --tenant tenant_alpha
    python -m lead_engine.cli reprocess --tenant tenant_alpha
    python -m lead_engine.cli metrics  --tenant tenant_alpha
    python -m lead_engine.cli demo     --tenant tenant_alpha   # full scripted run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .metrics import funnel as funnel_mod
from .approval import human_gate
from .config import get_tenant
from .service import build_service
from .sources.readers import read_source


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_ingest(args) -> int:
    svc = build_service(args.tenant)
    try:
        records = read_source(args.path, args.format or Path(args.path).suffix.lstrip("."))
        report = svc.pipeline.ingest_many(records, args.format or Path(args.path).suffix.lstrip("."))
        _print({"ingested": report.processed, "duplicates_merged": report.duplicates,
                "dropped_idempotent_sources": report.dropped_duplicate_sources})
        return 0
    finally:
        svc.close()


def cmd_qualify(args) -> int:
    svc = build_service(args.tenant)
    try:
        report = svc.pipeline.run_qualification()
        _print({"qualified": report.processed, "by_stage": report.by_stage})
        return 0
    finally:
        svc.close()


def cmd_queue(args) -> int:
    svc = build_service(args.tenant)
    try:
        _print(human_gate.pending_queue(svc.repo))
        return 0
    finally:
        svc.close()


def cmd_review(args) -> int:
    svc = build_service(args.tenant)
    try:
        _print(human_gate.review_view(svc.repo, args.lead))
        return 0
    finally:
        svc.close()


def cmd_approve(args) -> int:
    svc = build_service(args.tenant)
    try:
        lead = human_gate.approve_lead(svc.repo, args.lead, actor=args.actor, note=args.note or "")
        _print({"approved": lead["id"], "stage": lead["stage"]})
        return 0
    finally:
        svc.close()


def cmd_reject(args) -> int:
    svc = build_service(args.tenant)
    try:
        lead = human_gate.reject_lead(svc.repo, args.lead, actor=args.actor, note=args.note or "")
        _print({"rejected": lead["id"], "stage": lead["stage"]})
        return 0
    finally:
        svc.close()


def cmd_deliver(args) -> int:
    svc = build_service(args.tenant)
    try:
        svc.delivery.enqueue_all_approved()
        summary = svc.delivery.process_all()
        _print({"delivery": summary, "outbox_entries": svc.outbox.count()})
        return 0
    finally:
        svc.close()


def cmd_reprocess(args) -> int:
    svc = build_service(args.tenant)
    try:
        _print({"reprocess": svc.delivery.reprocess_dlq()})
        return 0
    finally:
        svc.close()


def cmd_metrics(args) -> int:
    svc = build_service(args.tenant)
    try:
        _print(funnel_mod.compute(svc.repo, svc.crm, svc.outbox).as_dict())
        return 0
    finally:
        svc.close()


def cmd_demo(args) -> int:
    """Scripted end-to-end demo on synthetic fixtures for one tenant."""
    tenant = args.tenant
    get_tenant(tenant)
    root = Path(__file__).resolve().parents[1]
    label = tenant.replace("tenant_", "")
    svc = build_service(tenant)
    try:
        # CSV + JSON record files
        for rel, fmt in [(f"fixtures/leads_{label}.csv", "csv"), (f"fixtures/leads_{label}.json", "json")]:
            path = root / rel
            if path.exists():
                svc.pipeline.ingest_many(read_source(path, fmt), fmt)
        # webhook envelopes (may carry cross-source duplicates of CSV leads)
        wh = root / f"fixtures/webhook_{label}.json"
        if wh.exists():
            for env in read_source(wh, "json"):
                svc.pipeline.ingest_raw(env["record"], "webhook", idempotency_hint=env.get("idempotency_key"))
        q = svc.pipeline.run_qualification()
        # auto-approve only clearly-safe approved_ready leads to demo delivery,
        # leave manual_review/injection/conflict for a human.
        approved = 0
        for lead in svc.repo.list_leads(stage="approved_ready"):
            human_gate.approve_lead(svc.repo, lead["id"], actor=f"auto-demo:{tenant}",
                                    note="demo auto-approval of approved_ready lead")
            approved += 1
        svc.delivery.enqueue_all_approved()
        delivery = svc.delivery.process_all()
        _print({
            "tenant": tenant,
            "qualified": q.processed,
            "auto_approved_ready": approved,
            "delivery": delivery,
            "awaiting_human": len(human_gate.pending_queue(svc.repo)),
            "metrics": funnel_mod.compute(svc.repo, svc.crm, svc.outbox).as_dict(),
        })
        return 0
    finally:
        svc.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lead_engine.cli", description="AthenAI Lead Engine (local, synthetic-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--tenant", required=True)

    sp = sub.add_parser("ingest"); common(sp)
    sp.add_argument("--path", required=True); sp.add_argument("--format", choices=["csv", "json"])
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("qualify"); common(sp); sp.set_defaults(func=cmd_qualify)
    sp = sub.add_parser("queue"); common(sp); sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("review"); common(sp); sp.add_argument("--lead", required=True); sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("approve"); common(sp); sp.add_argument("--lead", required=True)
    sp.add_argument("--actor", required=True); sp.add_argument("--note"); sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("reject"); common(sp); sp.add_argument("--lead", required=True)
    sp.add_argument("--actor", required=True); sp.add_argument("--note"); sp.set_defaults(func=cmd_reject)

    sp = sub.add_parser("deliver"); common(sp); sp.set_defaults(func=cmd_deliver)
    sp = sub.add_parser("reprocess"); common(sp); sp.set_defaults(func=cmd_reprocess)
    sp = sub.add_parser("metrics"); common(sp); sp.set_defaults(func=cmd_metrics)

    sp = sub.add_parser("demo"); common(sp); sp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except human_gate.ApprovalError as e:
        print(f"approval gate: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
