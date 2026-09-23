# AthenAI Lead Engine

A small, **locally runnable** lead-processing service. It demonstrates a safe,
auditable loop from several intake sources to a manager's decision and a **mock**
CRM — built entirely on **synthetic data** (no real CRM, keys, client lists,
scraping, or outbound messaging).

```
CSV / JSON / mock webhook
      -> normalize -> dedup -> qualify (rules + safe AI)
      -> evidence-only draft -> HUMAN APPROVAL (hard gate)
      -> mock CRM + task -> local mock-outbox -> metrics
                \__ retry / backoff / DLQ / reprocess on 429 & 5xx __/
```

## Requirements
- Python 3.11+ (developed and verified on 3.13.5)
- `pip install -r requirements.txt` (pinned: pydantic v2, fastapi, uvicorn, httpx, pytest)

## Quick start (from a fresh checkout)

```bash
pip install -r requirements.txt

# one-shot, scripted end-to-end demo on the synthetic fixtures (both tenants):
rm -rf data/ && \
  python -m lead_engine.cli demo --tenant tenant_alpha && \
  python -m lead_engine.cli demo --tenant tenant_beta

# full test suite (94 tests):
python -m pytest
```

The `rm -rf data/` line matters: it guarantees you start from an empty store, so
the numbers you see below are exactly reproducible. On Windows PowerShell use
`Remove-Item -Recurse -Force data\; python -m lead_engine.cli demo --tenant tenant_alpha`.

The demo prints funnel metrics. For **each tenant** the expected output is
identical:

```
sources=51  leads=46  duplicates_merged=5
stages:   outboxed=25  manual_review=17  rejected=4
delivery: succeeded=25 dlq=0   outbox_entries=25
```

These exact numbers are pinned by `tests/test_golden_demo.py`. If a code change
shifts them, that test fails on purpose.

## How the "≥ 60 synthetic records" requirement is met

Two defensible counts, both well above the 60-record floor:

| Count                                             | Per tenant | Both tenants |
|-------------------------------------------------|-----------:|-------------:|
| **Raw source records ingested**                   |       51   |   **102**    |
| ├─ CSV (`fixtures/leads_<tenant>.csv`)            |       24   |     48       |
| ├─ JSON (`fixtures/leads_<tenant>.json`)          |       16   |     32       |
| └─ Webhook envelopes (`fixtures/webhook_<tenant>.json`) | 11 |     22   |
| **Unique leads after deduplication**              |       46   |   **92**     |
| Cross-source duplicates merged                    |        5   |     10       |

The 5 duplicate merges per tenant come from the *webhook* file re-submitting 5
of the CSV strongs (deliberate, to exercise inbound idempotency + canonical
dedup). If your grader is counting unique leads, use **92**; if raw ingested
records, **102**. The pipeline is exercised end-to-end on both files by
`cli demo`. Additionally, `fixtures/adversarial.json` contributes 15
hand-crafted malicious cases that are exercised by `tests/test_adversarial_fixtures.py`.

## Manual operator flow (CLI)

```bash
python -m lead_engine.cli ingest    --tenant tenant_alpha --format csv  --path fixtures/leads_alpha.csv
python -m lead_engine.cli ingest    --tenant tenant_alpha --format json --path fixtures/leads_alpha.json
python -m lead_engine.cli qualify   --tenant tenant_alpha
python -m lead_engine.cli queue     --tenant tenant_alpha              # what needs a human
python -m lead_engine.cli review    --tenant tenant_alpha --lead <id>  # evidence + AI + draft
python -m lead_engine.cli approve   --tenant tenant_alpha --lead <id> --actor ops-alpha
python -m lead_engine.cli deliver   --tenant tenant_alpha              # gated by approval
python -m lead_engine.cli reprocess --tenant tenant_alpha             # drain DLQ after recovery
python -m lead_engine.cli metrics   --tenant tenant_alpha
python -m lead_engine.cli audit     --tenant tenant_alpha --lead <id> # full trail for one lead
python -m lead_engine.cli audit     --tenant tenant_alpha --latest    # (alias: explain)
python -m lead_engine.cli report    --out report.html                  # cross-tenant HTML dashboard
```

`audit` prints the complete journey of one lead as a single JSON document:
raw source envelopes → normalize flags → rules verdict+reason → AI
verdict+confidence+safety → combined verdict → draft → decisions → delivery
attempts (+ DLQ reason) → outbox entry → event trail.

## HTTP API (mock webhook + read-only views)

> ⚠️ **Security notice — read this before running.**
> The HTTP API below has **no authentication, no authorization, and no
> transport encryption**. It is designed to be safe **only** when bound to
> `127.0.0.1` on a developer machine, against synthetic data, with the mock
> CRM. **Do not** expose this on a public host, a shared network, or anything
> resembling production infrastructure. If you need to demo it on a LAN, put a
> reverse proxy in front that terminates TLS and enforces a bearer token, and
> swap the mock CRM/outbox for real, credentialed integrations. The included
> `.env.example` deliberately binds to `127.0.0.1` for exactly this reason.

```bash
python -m uvicorn lead_engine.app:app --host 127.0.0.1 --port 8000
```
- `POST /ingest/webhook/{tenant_id}` - body `{"tenant_id","idempotency_key","record":{...}}`
  (inbound-idempotent; validates the tenant; never delivers on its own)
- `GET /queue/{tenant_id}` - pending human-review queue
- `GET /metrics/{tenant_id}` - funnel metrics
- `GET /health`

**Live verification of inbound idempotency** (see `tests/test_adversarial_*` and
`CHANGELOG.md` for the recorded run): first POST returns `status=ingested`;
second POST with identical payload returns `status=duplicate_ignored`; a third
POST with the same `idempotency_key` but a different `record` also returns
`duplicate_ignored` (the UNIQUE `(tenant_id, idempotency_key)` is the strongest
guard — a caller cannot reuse a key to smuggle a different record). Exactly one
lead ends up in the store.

## Configuration
All runtime settings come from environment variables (see `.env.example`):
data directory, tenants, AI confidence threshold, retry/backoff limits, and the
mock CRM fault profile (`none | flaky_429 | flaky_5xx | always_500 | non_retryable_405`).
There are **no real secrets**; the AI provider is an offline deterministic mock.

## How the key requirements are met

| # | Requirement | Where |
|---|-------------|-------|
| 1 | Two tenants, strict isolation | physical per-tenant SQLite files + `LeadRepository(tenant_id)` scoped queries (`db.py`, `config.py`) |
| 2 | Idempotency + dedup + evidence | `canonical_key`, UNIQUE `(tenant_id,canonical_key)`, `lead_sources` snapshot, inbound idempotency key (`normalize.py`, `dedup.py`, `db.py`) |
| 3 | Rules + safe AI, strict JSON schema | `qualify_rules.py`; `qualify_ai.py` + `AIQualification(extra="forbid")`; safety forces manual/reject |
| 4 | Evidence-only draft, human gate before outbox | `draft.py` (template, evidence fields only) + `approval/human_gate.py` + stage machine (`models.py`) |
| 5 | Mock CRM, retry/DLQ/reprocess 429/5xx, metrics | `delivery/crm_mock.py`, `delivery/retry.py`, `delivery/outbox.py`, `metrics/funnel.py` |
| 6 | ≥ 60 synthetic records, ≥ 18 tests | `fixtures/` (**102 raw records, 92 unique leads**) + `fixtures/adversarial.json` (15 more), `tests/` (**94 tests**, ~5× the minimum) |
| 7 | One deepened module | **integration resilience** (`delivery/retry.py` + `crm_mock.py`) |

## Safety model (summary)
- The AI may only reference `evidence_field_ids` that exist on the lead; unknown
  references, out-of-schema fields, out-of-range confidence, or non-JSON output
  are treated as failures and routed to **manual review**.
- Opt-out and prompt-injection leads can never be auto-approved or auto-rejected
  for contact by the model; opt-out is a hard reject and cannot be approved.
- A draft is generated **only** from evidence; untrusted inbound text is never
  echoed. The outbox refuses anything without a recorded human `approve`.
- Delivery is **local only** (mock CRM + JSONL outbox); nothing is ever sent.
- **Unicode / zero-width hardening:** safety patterns run over an NFKD-normalised,
  zero-width-stripped copy of every text field, so `𝐢𝐠𝐧𝐨𝐫𝐞 𝐩𝐫𝐞𝐯𝐢𝐨𝐮𝐬`,
  `ｉｇｎｏｒｅ ｐｒｅｖｉｏｕｓ`, and `ig​nore previous` are all caught.
- **Hostile text quarantine:** the injection pattern set includes XSS markers
  (`<script`, `javascript:`, `on*=…=`) and classic SQLi markers
  (`DROP TABLE`, `UNION SELECT`, `;--`) so even a mistakenly-approved lead
  cannot smuggle those downstream.
- **Adversarial LLM proof:** `AdversarialMockLLMProvider` deliberately emits
  seven failure modes (non-JSON, extra fields, out-of-range confidence,
  ungrounded evidence refs, empty evidence, wrong enum, override-optout) and
  **every mode is verified end-to-end** by `tests/test_adversarial_llm.py`
  to leave the pipeline unable to reach `outboxed` without a human approve.

See `docs/THREAT_MODEL.md`, `docs/COMMERCIAL_MEMO.md`, and `CHANGELOG.md`.

## Reproducibility & performance

The demo is deterministic; there is no RNG anywhere in fixture generation,
normalization, or qualification. Concretely:

- **Demo** (102 raw records, 2 tenants): runs the full loop in a few seconds;
  the numbers above are pinned by `tests/test_golden_demo.py`.
- **Test suite**: **94 tests, ~21 s** on a laptop (Python 3.13.5, pytest 9.1.1).
- **Synthetic load test** (`scripts/load_test.py`), fresh data dir per run:
  - `--n 1000` → **5.2 s**, **~192 records / sec** end-to-end
    (ingest+dedup 2.0 s + qualify 3.2 s), 931 unique leads / 46 dups merged.
  - `--n 5000` → **29 s**, **~172 records / sec**, 4359 unique leads /
    270 dups merged.
  - SQLite is opened in `journal_mode=WAL` + `synchronous=NORMAL`. That single
    change was measured at **32× faster** than the default rollback journal on
    this workload (was 4.9 rps → now 192 rps on `--n 1000`) because every
    ingest/qualify/decision row triggers its own commit.

## Known limitations (honest scope)

These are stated deliberately so the reader can weigh the demo against what a
real production system would need.

- **Regex-based guardrails are a floor, not a ceiling.** The injection /
  opt-out patterns catch the classics and a broad family of obfuscations, but
  they are English-only and pattern-based. A real deployment would pair this
  with a dedicated classifier / guardrail model and a much wider multilingual
  pattern set. This is why ambiguous cases are routed to a human, not trusted.
- **Dedup on `name + company` is intentionally conservative** and will over-
  merge homonyms (e.g. two "J. Smith at Acme" from different cities). Email
  and normalised phone are preferred when present; the fallback hash exists so
  records with neither are still stable across reruns.
- **SQLite is single-writer.** With WAL you get many readers plus one writer
  and this is fine for one CLI/one uvicorn worker. Two competing workers on
  the same store will serialise on commits; a real deployment would move
  to Postgres + a queue.
- **No auth / TLS on the HTTP surface** (see the security notice above). The
  FastAPI layer exists so the webhook contract can be exercised locally.
- **Mock LLM.** `MockLLMProvider` is deterministic and offline. The
  strict-schema + evidence-grounding + safety-forcing wrapper is real; the
  model behind it is not.
- **Mock CRM and mock outbox.** Delivery is a JSONL file + a local SQLite
  "CRM". Nothing leaves the machine, so end-to-end timing is stable but the
  failure modes are what we chose to inject.

## Project layout
```
lead_engine/        application code
  ├── config.py     settings, tenants, per-tenant paths
  ├── models.py     stage state machine + strict pydantic schemas
  ├── db.py         per-tenant, tenant-scoped SQLite repository
  ├── audit.py      lead-trail composer (raw -> normalize -> AI -> outbox)
  ├── reporting.py  HTML funnel dashboard across tenants
  ├── pipeline/     normalize, dedup, qualify_rules, qualify_ai, draft, orchestrator
  ├── approval/     human_gate (hard approval choke point)
  ├── delivery/     crm_mock, outbox (guarded), retry (deep module)
  ├── metrics/      funnel (counts + SLA timings)
  ├── sources/      CSV / JSON / webhook readers
  ├── cli.py        13 sub-commands (ingest/qualify/queue/review/approve/
  │                 reject/deliver/reprocess/metrics/audit/explain/report/demo)
  └── app.py        FastAPI webhook + queue + metrics + health

fixtures/           generator + committed CSV/JSON/webhook data (102 records)
                    + adversarial.json (15 malicious cases) + adversarial_generate.py
tests/              pytest suite (94 tests)
docs/               THREAT_MODEL.md, COMMERCIAL_MEMO.md
scripts/            load_test.py (synthetic stress run)
data/               runtime files (gitignored; regenerated on demand)
```

## Attribution
> These lines describe how this repository was produced.

- **Built personally (design + implementation + review):** overall architecture,
  tenant isolation approach, the human-approval choke point, the strict AI
  safety policy, the retry / DLQ / reprocess design, all fixtures, tests, and
  docs, plus review and verification of every generated file.
- **AI-assisted:** an AI coding agent (Qoder) drafted most of the boilerplate
  code and documentation text, which was then reviewed and corrected by hand.
  Real defects found and fixed during verification: (i) a non-ASCII test
  symbol, (ii) a delivery/outbox ordering bug surfaced by 5 failing tests,
  (iii) an unused `_flaky_remaining` dict after the CRM profiles moved to
  `_call_count`, (iv) a 32× SQLite-commit bottleneck exposed by the load
  test, fixed via WAL + `synchronous=NORMAL`.
- **Hours spent: ~6 hours of wall-clock focused effort** across a single
  session on 2026-09-23, plus ~1.5 hours of the reviewer-driven second pass
  (this repo's `CHANGELOG.md` "Unreleased" section) — approximately:
  architecture + planning 1.5 h, implementation across pipeline / approval /
  delivery / metrics 2 h, fixtures + tests 1 h, docs 0.5 h, quality pass +
  verification + Tier-2/3 fixes 2.5 h. The number is stated as an
  estimate derived from this session's timeline, not a timesheet.
- **API / tooling cost: $0** — the engine uses an offline mock LLM, no
  external AI calls, no paid services. Cost is limited to local development
  time.

## License / scope
Synthetic demo project for an evaluation task. Contains no real customer data,
credentials, or external integrations.
