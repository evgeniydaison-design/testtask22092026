# Commercial Memo - AthenAI Lead Engine

**Audience:** decision-maker evaluating a governed lead-operations capability.
**Status:** working local prototype on synthetic data (this repository).

## Problem
Sales teams pull leads from many channels (forms, CSV imports, partner webhooks).
Raw tools either (a) spam prospects with unreviewed, low-quality outreach or
(b) bury good leads in manual triage. Teams also need tenant separation, a full
audit trail, and resilience when downstream CRM APIs throttle or fail.

## What this demonstrates
A complete, controllable lead loop:

`intake (CSV/JSON/webhook) -> normalize -> dedup -> qualify (rules + AI) ->
human approval -> mock CRM + task -> local outbox -> metrics`

with four properties that are hard to get right at once:

1. **Safety by construction.** The AI cannot send anything. It may only reason
   over *evidence*; anything doubtful, conflicting, opted-out, or injection-laced
   is forced to a human. A message draft is generated only from verified evidence
   and physically cannot reach the outbox without an explicit human `approve`.
2. **Auditable multi-tenancy.** Two tenants are strictly isolated (separate
   storage + scoped data access), with source/evidence and every decision kept.
3. **Operationally resilient.** Idempotent intake and delivery, plus a
   retry/backoff/DLQ/reprocess layer for 429/5xx - no lost or duplicated work.
4. **Measurable.** Funnel metrics (qualification mix, manual-review rate, AI
   safety flags, delivery outcomes) are available from day one.

## Evidence in this build
- 102 synthetic records across all intake types and edge cases (duplicates,
  opt-out, prompt-injection, conflicts, thin data).
- 44 automated tests, all green, covering isolation, idempotency, the AI safety
  policy, the human gate, and the resilience module.
- A reproducible one-command demo: `python -m lead_engine.cli demo --tenant tenant_alpha`.
- A written threat model mapping each risk to its control and test.

## Positioning / value
- **Risk reduction:** keeps a human on the outbound trigger - the single most
  important control for brand and compliance.
- **Speed to value:** the pipeline, rules, and metrics are already wired; real
  sources and a real CRM are drop-in adapters, not a rebuild.
- **Trust:** provable tenant isolation and a full evidence/decision trail.

## Scope and next steps (for a paid engagement)
This is deliberately a mock, offline system. To productionize:
1. Replace mock CRM/outbox with authenticated connectors + real idempotency keys.
2. Point the AI adapter at a governed model endpoint; keep the strict schema +
   evidence-grounding + human gate unchanged.
3. Add authN/authZ, secret management, at-rest encryption, and rate limiting.
4. Calibrate qualification thresholds and the rules/AI/human mix on real data.

**Cost to date:** local development only; the prototype uses an offline mock LLM,
so API spend is $0. No real customer data, keys, scraping, or external messaging
were used at any point.

---
*Prepared as a self-contained evaluation artifact. Personal-effort and hours
figures are recorded in `README.md` (Attribution) and should be confirmed.*
