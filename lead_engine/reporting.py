"""Static HTML dashboard for cross-tenant funnel metrics.

Produces a single self-contained HTML file (no external CSS/JS) with a compact
visual funnel per tenant, delivery/DLQ counters, and the operator-queue SLA.
The point is readability for a reviewer or a product manager: opening one file
is much more convincing than scrolling through JSON.
"""

from __future__ import annotations

import html

from .metrics.funnel import FunnelMetrics


_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 24px;
       background: #fafafa; color: #222; }
h1 { margin: 0 0 4px 0; font-size: 22px; }
.sub { color: #666; font-size: 13px; margin-bottom: 18px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.card { background: #fff; border: 1px solid #e2e2e2; border-radius: 8px;
        padding: 16px 18px; box-shadow: 0 1px 0 rgba(0,0,0,0.02); }
.card h2 { margin: 0 0 6px 0; font-size: 17px; }
.stage { display: flex; align-items: center; margin: 6px 0; font-size: 13px; }
.stage .name { width: 130px; color: #444; }
.stage .bar { flex: 1; height: 18px; background: #eee; border-radius: 4px; overflow: hidden; }
.stage .fill { height: 100%; border-radius: 4px; }
.stage .count { width: 50px; text-align: right; margin-left: 8px; font-variant-numeric: tabular-nums; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin-top: 10px; font-size: 13px; }
.kv b { color: #444; font-weight: 600; }
.green { background: #35a35a; } .amber { background: #d59a2b; } .red { background: #c54b4b; }
.blue { background: #4a86d4; } .grey { background: #8f8f8f; }
.footer { margin-top: 22px; color: #888; font-size: 11px; }
code { background: #f2f2f2; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
         color: #fff; margin-left: 6px; }
"""

_STAGE_COLOR = {
    "received": "grey", "normalized": "grey", "deduped": "grey",
    "approved_ready": "blue", "manual_review": "amber", "rejected": "red",
    "human_approved": "blue", "human_rejected": "red",
    "crm_synced": "green", "dlq": "red", "outboxed": "green",
}


def _bar_row(name: str, count: int, total: int) -> str:
    pct = 0 if total <= 0 else round(100 * count / total, 1)
    color = _STAGE_COLOR.get(name, "grey")
    return (
        f'<div class="stage"><div class="name">{html.escape(name)}</div>'
        f'<div class="bar"><div class="fill {color}" style="width: {pct}%"></div></div>'
        f'<div class="count">{count}</div></div>'
    )


def _card(m: FunnelMetrics) -> str:
    total = max(m.leads_total, 1)
    rows = "".join(_bar_row(k, v, total) for k, v in m.stage_counts.items())
    sla_a = m.avg_seconds_ingest_to_decision
    sla_b = m.avg_seconds_in_manual_review
    dlq_badge = (
        f'<span class="badge red">{m.delivery.get("dlq", 0)} dlq</span>'
        if m.delivery.get("dlq", 0) else '<span class="badge green">0 dlq</span>'
    )
    hist = ", ".join(f"{k}×{v}" for k, v in sorted(m.crm_status_histogram.items())) or "—"
    return f"""
      <div class="card">
        <h2>{html.escape(m.tenant_id)}
          <span class="badge blue">{m.leads_total} leads</span>
          <span class="badge amber">{m.stage_counts.get("manual_review", 0)} in review</span>
          {dlq_badge}
        </h2>
        {rows}
        <div class="kv">
          <b>Raw source records</b><span>{m.sources}</span>
          <b>Duplicates merged</b><span>{m.duplicates_merged}</span>
          <b>manual_review_rate</b><span>{m.manual_review_rate:.3f}</span>
          <b>AI safety-flagged</b><span>{m.ai_safety_flagged}</span>
          <b>Delivery</b><span>succeeded={m.delivery.get("succeeded", 0)},
                                     retrying={m.delivery.get("retrying", 0)},
                                     dlq={m.delivery.get("dlq", 0)}</span>
          <b>CRM status histogram</b><span><code>{html.escape(hist)}</code></span>
          <b>Outbox entries</b><span>{m.outbox_entries}</span>
          <b>Avg ingest→decision (s)</b><span>{"—" if sla_a is None else sla_a}</span>
          <b>Avg time in manual_review (s)</b><span>{"—" if sla_b is None else sla_b}</span>
        </div>
      </div>
    """


def render_dashboard(metrics: list[FunnelMetrics]) -> str:
    cards = "".join(_card(m) for m in metrics)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AthenAI Lead Engine - funnel report</title>
<style>{_CSS}</style></head>
<body>
  <h1>AthenAI Lead Engine - funnel report</h1>
  <div class="sub">
    Local, offline, synthetic-only. Data is per-tenant and never leaves this machine.
    Regenerate with <code>python -m lead_engine.cli report --out report.html</code>.
  </div>
  <div class="grid">{cards}</div>
  <div class="footer">
    Colors: grey = pre-verdict stages, blue = approved (pre-delivery),
    amber = awaiting human, green = delivered, red = rejected / DLQ.
  </div>
</body></html>
"""
