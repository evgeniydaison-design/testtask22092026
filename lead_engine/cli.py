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
from . import audit_chain, calibration, provenance
from . import fuzz as fuzz_mod
from .audit import explain_lead, subject_export
from .config import get_tenant, load_settings
from .reporting import render_dashboard
from .run_report import render_run_explainer
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


def cmd_audit(args) -> int:
    """Print the full explain-trail for one lead (or --latest for the most
    recently updated lead in this tenant)."""
    if not args.lead and not args.latest:
        print("error: pass --lead <id> or --latest", file=sys.stderr)
        return 2
    svc = build_service(args.tenant)
    try:
        if args.latest:
            leads = svc.repo.list_leads()
            if not leads:
                print("no leads in this tenant", file=sys.stderr)
                return 1
            lead_id = max(leads, key=lambda l: l.get("updated_at") or "")["id"]
        else:
            lead_id = args.lead
        _print(explain_lead(svc.repo, svc.crm, svc.outbox, lead_id))
        return 0
    finally:
        svc.close()


def cmd_report(args) -> int:
    """Generate a self-contained HTML funnel dashboard across all known tenants."""
    settings = load_settings()
    metrics_list = []
    for tenant in settings.tenants:
        svc = build_service(tenant)
        try:
            metrics_list.append(funnel_mod.compute(svc.repo, svc.crm, svc.outbox))
        finally:
            svc.close()
    html = render_dashboard(metrics_list)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    _print({"wrote": str(out.resolve()), "tenants": [m.tenant_id for m in metrics_list]})
    return 0


def cmd_audit_seal(args) -> int:
    """Fold this tenant's events+decisions into a SHA-256 chain and seal it."""
    settings = load_settings()
    svc = build_service(args.tenant)
    try:
        rec = audit_chain.seal(svc.repo, settings.audit_seal_path(args.tenant))
        _print(rec)
        return 0
    finally:
        svc.close()


def cmd_verify_audit(args) -> int:
    """Rebuild the audit chain and compare it to the seal (tamper detection)."""
    settings = load_settings()
    svc = build_service(args.tenant)
    try:
        res = audit_chain.verify(svc.repo, settings.audit_seal_path(args.tenant))
        _print(res)
        return 3 if res["status"] == "TAMPERED" else 0
    finally:
        svc.close()


def cmd_calibrate(args) -> int:
    """Rules-vs-AI disagreement matrix + confidence-threshold what-if sweep."""
    svc = build_service(args.tenant)
    try:
        _print(calibration.compute(svc.pipeline))
        return 0
    finally:
        svc.close()


def cmd_fuzz(args) -> int:
    """Generate many adversarial leads and prove nothing is delivered/approved
    without a human. Exits non-zero if the invariant is ever violated."""
    settings = load_settings()
    rep = fuzz_mod.run_fuzz(
        settings.data_dir / "_fuzz", n=args.n, seed=args.seed,
        ai_threshold=settings.ai_confidence_threshold,
    )
    _print(rep.as_dict())
    return 0 if rep.invariant_ok else 3


def cmd_subject_export(args) -> int:
    """DSAR export: everything held about one subject (email) within a tenant."""
    svc = build_service(args.tenant)
    try:
        _print(subject_export(svc.repo, svc.crm, svc.outbox, args.email))
        return 0
    finally:
        svc.close()


def cmd_provenance(args) -> int:
    """Print the reproducibility fingerprint (git + fixtures + config)."""
    _print(provenance.collect(load_settings()))
    return 0


def cmd_explain_run(args) -> int:
    """Write ONE self-contained HTML that explains the entire run."""
    settings = load_settings()
    metrics, calibs, chains = [], [], []
    for tenant in settings.tenants:
        svc = build_service(tenant)
        try:
            metrics.append(funnel_mod.compute(svc.repo, svc.crm, svc.outbox))
            calibs.append(calibration.compute(svc.pipeline))
            chains.append(audit_chain.verify(svc.repo, settings.audit_seal_path(tenant)))
        finally:
            svc.close()
    fz = None
    if not args.no_fuzz:
        rep = fuzz_mod.run_fuzz(
            settings.data_dir / "_fuzz", n=args.fuzz_n, seed=20260923,
            ai_threshold=settings.ai_confidence_threshold,
        )
        fz = rep.as_dict()
    prov = provenance.collect(settings)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_run_explainer(metrics, calibs, chains, fz, prov), encoding="utf-8")
    _print({
        "wrote": str(out.resolve()),
        "tenants": [m.tenant_id for m in metrics],
        "fuzz_invariant_ok": (fz or {}).get("invariant_ok"),
        "chains": {c["tenant_id"]: c["status"] for c in chains},
    })
    return 0


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
        metrics = funnel_mod.compute(svc.repo, svc.crm, svc.outbox)
        # seal the audit chain so verify-audit / explain-run can prove integrity
        seal_rec = audit_chain.seal(svc.repo, svc.settings.audit_seal_path(tenant))
        _print({
            "tenant": tenant,
            "qualified": q.processed,
            "auto_approved_ready": approved,
            "delivery": delivery,
            "awaiting_human": len(human_gate.pending_queue(svc.repo)),
            "metrics": metrics.as_dict(),
            "audit_chain": {
                "status": "sealed",
                "links": seal_rec["count"],
                "head": seal_rec["head"],
                "seal_file": svc.settings.audit_seal_path(tenant).name,
            },
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

    sp = sub.add_parser("audit", help="full explain-trail for one lead")
    common(sp); sp.add_argument("--lead", required=False, default=None)
    sp.add_argument("--latest", action="store_true",
                   help="pick the most-recently-updated lead instead of --lead")
    sp.set_defaults(func=cmd_audit)
    # 'explain' is a friendlier alias for the same thing
    sp = sub.add_parser("explain", help="alias of audit")
    common(sp); sp.add_argument("--lead", required=False, default=None)
    sp.add_argument("--latest", action="store_true")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("report", help="write a cross-tenant HTML funnel dashboard")
    sp.add_argument("--out", default="report.html")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("audit-seal", help="seal the tenant's tamper-evident audit chain")
    common(sp); sp.set_defaults(func=cmd_audit_seal)

    sp = sub.add_parser("verify-audit", help="verify the audit chain against the seal")
    common(sp); sp.set_defaults(func=cmd_verify_audit)

    sp = sub.add_parser("calibrate", help="rules-vs-AI matrix + confidence-threshold sweep")
    common(sp); sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("fuzz", help="adversarial invariant proof (nothing delivered without a human)")
    sp.add_argument("--n", type=int, default=500)
    sp.add_argument("--seed", type=int, default=20260923)
    sp.set_defaults(func=cmd_fuzz)

    sp = sub.add_parser("subject-export", help="DSAR export for one email in a tenant")
    common(sp); sp.add_argument("--email", required=True); sp.set_defaults(func=cmd_subject_export)

    sp = sub.add_parser("provenance", help="print the reproducibility fingerprint")
    sp.set_defaults(func=cmd_provenance)

    sp = sub.add_parser("explain-run", help="write ONE self-contained HTML explaining the run")
    sp.add_argument("--out", default="run.html")
    sp.add_argument("--fuzz-n", type=int, default=500, dest="fuzz_n")
    sp.add_argument("--no-fuzz", action="store_true", dest="no_fuzz")
    sp.set_defaults(func=cmd_explain_run)

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
