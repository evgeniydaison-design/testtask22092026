"""Self-contained "explain this run" HTML report.

Where ``reporting.render_dashboard`` is just the funnel, this assembles the
whole engineering story into ONE file a reviewer can open and immediately trust:

  * per-tenant funnel cards (reused from the dashboard),
  * a state-machine diagram **generated from ``models.VALID_TRANSITIONS``** -
    the exact same table the runtime enforces - with the human-gated edges
    highlighted, so the diagram cannot drift from the code,
  * the rules-vs-AI disagreement matrix + threshold what-if sweep,
  * an adversarial-fuzz invariant verdict,
  * the tamper-evident audit-chain status per tenant,
  * a provenance fingerprint (git commit + fixtures digest + config).

Stdlib only; no external CSS/JS/fonts/images. Everything is inline SVG + HTML.
"""

from __future__ import annotations

import html

from .metrics.funnel import FunnelMetrics
from .models import VALID_TRANSITIONS, LeadStage
from .reporting import _CSS, _card

# Layout for the state-machine diagram (top-left of each node box).
_STAGE_POS: dict[str, tuple[int, int]] = {
    "received": (24, 108), "normalized": (168, 108), "deduped": (312, 108),
    "approved_ready": (470, 34), "manual_review": (470, 120), "rejected": (470, 206),
    "human_approved": (636, 60), "human_rejected": (636, 206),
    "crm_synced": (800, 12), "dlq": (800, 96), "outboxed": (930, 54),
}
_NW, _NH = 120, 28
_CANVAS_W, _CANVAS_H = 1072, 252

_NODE_FILL = {
    "received": "#8f8f8f", "normalized": "#8f8f8f", "deduped": "#8f8f8f",
    "approved_ready": "#4a86d4", "manual_review": "#d59a2b", "rejected": "#c54b4b",
    "human_approved": "#7e57c2", "human_rejected": "#c54b4b",
    "crm_synced": "#35a35a", "dlq": "#c54b4b", "outboxed": "#35a35a",
}


def _center(stage: str) -> tuple[int, int]:
    x, y = _STAGE_POS[stage]
    return x + _NW // 2, y + _NH // 2


def _edge_class(frm: str, to: str) -> str:
    """Human-gated edges into approval are red; post-approval delivery green."""
    if to == LeadStage.HUMAN_APPROVED.value:
        return "gated"
    if frm in (LeadStage.HUMAN_APPROVED.value, LeadStage.CRM_SYNCED.value, LeadStage.DLQ.value):
        return "post"
    return "auto"


def state_machine_svg() -> str:
    parts = [
        f'<svg viewBox="0 0 {_CANVAS_W} {_CANVAS_H}" width="100%" '
        'xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, sans-serif">'
    ]
    parts.append(
        "<defs><marker id='arw' markerWidth='9' markerHeight='9' refX='7' refY='3' "
        "orient='auto'><path d='M0,0 L7,3 L0,6 Z' fill='#666'/></marker>"
        "<marker id='arwR' markerWidth='9' markerHeight='9' refX='7' refY='3' "
        "orient='auto'><path d='M0,0 L7,3 L0,6 Z' fill='#c54b4b'/></marker>"
        "<marker id='arwG' markerWidth='9' markerHeight='9' refX='7' refY='3' "
        "orient='auto'><path d='M0,0 L7,3 L0,6 Z' fill='#2e7d32'/></marker></defs>"
    )
    arrow = {"gated": "arwR", "post": "arwG", "auto": "arw"}
    color = {"gated": "#c54b4b", "post": "#2e7d32", "auto": "#999"}
    # edges first (so nodes sit on top)
    for frm, targets in VALID_TRANSITIONS.items():
        f = frm.value
        if f not in _STAGE_POS:
            continue
        fx, fy = _center(f)
        for to in targets:
            t = to.value
            if t not in _STAGE_POS:
                continue
            tx, ty = _center(t)
            cls = _edge_class(f, t)
            dash = " stroke-dasharray='5,3'" if cls == "gated" else ""
            parts.append(
                f"<line x1='{fx}' y1='{fy}' x2='{tx}' y2='{ty}' stroke='{color[cls]}' "
                f"stroke-width='1.6'{dash} marker-end='url(#{arrow[cls]})' opacity='0.8'/>"
            )
    # nodes
    for stage, (x, y) in _STAGE_POS.items():
        fill = _NODE_FILL.get(stage, "#8f8f8f")
        parts.append(
            f"<rect x='{x}' y='{y}' width='{_NW}' height='{_NH}' rx='5' "
            f"fill='{fill}' opacity='0.92'/>"
            f"<text x='{x + _NW // 2}' y='{y + _NH // 2 + 4}' text-anchor='middle' "
            f"fill='#fff' font-size='12'>{html.escape(stage)}</text>"
        )
    parts.append("</svg>")
    legend = (
        "<div class='legend'>"
        "<span><i style='background:#c54b4b'></i> requires a human click (🔒)</span>"
        "<span><i style='background:#2e7d32'></i> post-approval delivery</span>"
        "<span><i style='background:#999'></i> automatic</span></div>"
    )
    return "<div class='svgwrap'>" + "".join(parts) + legend + "</div>"


def _calibration_section(calibs: list[dict]) -> str:
    blocks = []
    for c in calibs:
        d = c["disagreement"]
        cal = c["calibration"]
        rows = d["matrix"]
        head = "<tr><th>rules \\ AI</th><th>approve</th><th>manual</th><th>reject</th></tr>"
        body = ""
        for r in ("approve_ready", "manual_review", "reject"):
            cells = "".join(f"<td>{rows[r][a]}</td>" for a in ("approve_ready", "manual_review", "reject"))
            body += f"<tr><th>{r}</th>{cells}</tr>"
        sweep_rows = ""
        for s in cal["sweep"]:
            mark = " ◀ current" if abs(s["threshold"] - cal["current_threshold"]) < 1e-9 else ""
            sweep_rows += (
                f"<tr><td>{s['threshold']}{mark}</td><td>{s['approve_ready']}</td>"
                f"<td>{s['manual_review']}</td><td>{s['reject']}</td><td>{s['auto_rate']:.2f}</td></tr>"
            )
        blocks.append(
            f"""<div class="card">
        <h2>{html.escape(c['tenant_id'])} · calibration</h2>
        <p class='note'>Rules and the guarded AI agreed on <b>{d['agree']}/{d['total']}</b> leads.
        They disagree productively: the AI would have auto-passed <b>{d['ai_more_lenient']}</b>
        lead(s) the rules conservatively held for a human, while the AI/grounding gate
        independently escalated <b>{d['ai_more_strict']}</b> the rules alone would have passed.
        The strict AND-guard means a lead only auto-approves when both layers agree.</p>
        <div class="two">
          <div><table class='tbl'>{head}{body}</table></div>
          <div><table class='tbl'>
            <tr><th>conf. threshold</th><th>approve</th><th>manual</th><th>reject</th><th>auto%</th></tr>
            {sweep_rows}</table>
            <p class='note'>Monotonic: {cal['monotonic']} · lowering the threshold only ever moves
            leads toward auto-approve, never hides an unsafe one.</p></div>
        </div>
      </div>"""
        )
    return "".join(blocks)


def _fuzz_section(f: dict | None) -> str:
    if not f:
        return ""
    ok = f.get("invariant_ok")
    cls = "ok" if ok else "bad"
    flags = f.get("flags", {})
    stages = f.get("stage_counts", {})
    stage_txt = ", ".join(f"{k}={v}" for k, v in sorted(stages.items())) or "—"
    return f"""<div class="card"><h2>Adversarial fuzz · invariant proof</h2>
      <p class="badge-{cls}">{'PASS' if ok else 'FAIL'}: nothing reached a
      delivery stage or the outbox without a human approval</p>
      <div class="kv">
        <b>Records generated</b><span>{f.get('n_records')} (seed {f.get('seed')})</span>
        <b>Unique leads</b><span>{f.get('unique_leads')}</span>
        <b>Safety detections</b><span>injection={flags.get('injection',0)},
          opt_out={flags.get('opt_out',0)}, conflict={flags.get('conflict',0)},
          needs_review={flags.get('needs_review',0)}</span>
        <b>Unapproved outbox writes</b><span>{f.get('unapproved_writes_blocked')}/{f.get('unapproved_write_attempts')} blocked by the gate</span>
        <b>Outbox entries</b><span>{f.get('outbox_entries')}</span>
        <b>Final stages</b><span><code>{html.escape(stage_txt)}</code></span>
      </div></div>"""


def _chain_section(chains: list[dict]) -> str:
    rows = ""
    for ch in chains:
        status = ch.get("status", "unknown")
        cls = "ok" if status == "verified" else ("bad" if status == "TAMPERED" else "warn")
        rows += (
            f"<tr><td>{html.escape(ch.get('tenant_id',''))}</td>"
            f"<td class='pill-{cls}'>{html.escape(status)}</td>"
            f"<td>{ch.get('current_count', ch.get('sealed_count', '—'))}</td>"
            f"<td><code>{html.escape(str(ch.get('current_head','')[:16]))}</code></td></tr>"
        )
    return f"""<div class="card"><h2>Tamper-evident audit chain</h2>
      <p class='note'>Each tenant's events + decisions are folded into a SHA-256 chain; the head is
      sealed outside the DB. Re-running <code>verify-audit</code> detects any after-the-fact edit,
      insert or delete of an approval/stage record.</p>
      <table class='tbl'><tr><th>tenant</th><th>chain status</th><th>links</th><th>head</th></tr>{rows}</table>
      </div>"""


def _provenance_section(prov: dict) -> str:
    cfg = prov.get("config", {})
    kv = "".join(f"<b>{html.escape(str(k))}</b><span>{html.escape(str(v))}</span>" for k, v in cfg.items())
    return f"""<div class="card"><h2>Provenance / reproducibility</h2>
      <div class="kv">
        <b>git commit</b><span><code>{html.escape(str(prov.get('git_commit')))}</code></span>
        <b>fixtures digest</b><span><code>{html.escape(str(prov.get('fixtures_digest')))}</code></span>
        <b>run fingerprint</b><span><code>{html.escape(str(prov.get('fingerprint')))}</code></span>
        <b>python</b><span>{html.escape(str(prov.get('python')))}</span>
        <b>platform</b><span>{html.escape(str(prov.get('platform')))}</span>
        {kv}
      </div></div>"""


_EXTRA_CSS = """
.tbl { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
.tbl th, .tbl td { border: 1px solid #ddd; padding: 3px 8px; text-align: center; }
.tbl th { background: #f4f4f4; font-weight: 600; }
.note { font-size: 12px; color: #555; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.svgwrap { background:#fff; border:1px solid #e2e2e2; border-radius:8px; padding:10px; }
.legend { font-size: 11px; color: #666; margin-top: 6px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; vertical-align:middle;}
.badge-ok { color:#1b7a3d; font-weight:600; } .badge-bad { color:#b02b2b; font-weight:600; }
.pill-ok { background:#e6f4ea; color:#1b7a3d; border-radius:8px; }
.pill-bad { background:#fdecea; color:#b02b2b; border-radius:8px; }
.pill-warn { background:#fef7e0; color:#8a6d1b; border-radius:8px; }
.section-h { margin: 26px 0 10px; font-size: 15px; color:#333; border-bottom:1px solid #ddd; padding-bottom:4px;}
"""


def render_run_explainer(
    metrics: list[FunnelMetrics],
    calibs: list[dict],
    chains: list[dict],
    fuzz: dict | None,
    prov: dict,
) -> str:
    cards = "".join(_card(m) for m in metrics)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AthenAI Lead Engine - run explainer</title>
<style>{_CSS}{_EXTRA_CSS}</style></head>
<body>
  <h1>AthenAI Lead Engine - explain this run</h1>
  <div class="sub">Local, offline, synthetic-only. One file that shows the funnel,
    the safety machine, the calibration, the adversarial invariant, the tamper-evident
    audit chain and the provenance fingerprint.
    Regenerate: <code>python -m lead_engine.cli explain-run --out run.html</code></div>

  <div class="section-h">1 · Stage machine (generated from VALID_TRANSITIONS)</div>
  {state_machine_svg()}

  <div class="section-h">2 · Funnel per tenant</div>
  <div class="grid">{cards}</div>

  <div class="section-h">3 · Rules-vs-AI calibration</div>
  <div class="grid">{_calibration_section(calibs)}</div>

  <div class="section-h">4 · Adversarial invariant</div>
  {_fuzz_section(fuzz)}

  <div class="section-h">5 · Tamper-evident audit chain</div>
  {_chain_section(chains)}

  <div class="section-h">6 · Provenance</div>
  {_provenance_section(prov)}

  <div class="footer">Colors: grey = pre-verdict, blue = approved (pre-delivery),
    amber = awaiting human, green = delivered, red = rejected / gated.</div>
</body></html>
"""
