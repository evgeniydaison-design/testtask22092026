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
- Python 3.11+ (developed/tested on 3.13)
- `pip install -r requirements.txt` (pydantic v2, fastapi, uvicorn, httpx, pytest)

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; sane local defaults are built in

# one-shot, scripted end-to-end demo on the synthetic fixtures
python -m lead_engine.cli demo --tenant tenant_alpha

# run the test suite (44 tests)
python -m pytest

# regenerate the synthetic fixtures (deterministic, no RNG)
python fixtures/generate.py
```

The demo prints funnel metrics. Typical output for `tenant_alpha`:

```
sources=51  leads=46  duplicates_merged=5
stages: outboxed=25, manual_review=17, rejected=4
delivery: succeeded=25 dlq=0   outbox_entries=25
```

## Manual operator flow (CLI)

```bash
python -m lead_engine.cli ingest   --tenant tenant_alpha --format csv --path fixtures/leads_alpha.csv
python -m lead_engine.cli ingest   --tenant tenant_alpha --format json --path fixtures/leads_alpha.json
python -m lead_engine.cli qualify  --tenant tenant_alpha
python -m lead_engine.cli queue    --tenant tenant_alpha           # what needs a human
python -m lead_engine.cli review   --tenant tenant_alpha --lead <id>   # evidence + AI + draft
python -m lead_engine.cli approve  --tenant tenant_alpha --lead <id> --actor ops-alpha
python -m lead_engine.cli deliver  --tenant tenant_alpha            # gated by approval
python -m lead_engine.cli reprocess --tenant tenant_alpha          # drain DLQ after recovery
python -m lead_engine.cli metrics  --tenant tenant_alpha
```

## HTTP API (mock webhook + read-only views)

```bash
python -m uvicorn lead_engine.app:app --host 127.0.0.1 --port 8000
```
- `POST /ingest/webhook/{tenant_id}` - body `{"tenant_id","idempotency_key","record":{...}}`
  (inbound-idempotent; validates the tenant; never delivers on its own)
- `GET /queue/{tenant_id}` - pending human-review queue
- `GET /metrics/{tenant_id}` - funnel metrics
- `GET /health`

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
| 6 | >=60 synthetic records, >=18 tests | `fixtures/` (102 records), `tests/` (44 tests) |
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

See `docs/THREAT_MODEL.md` and `docs/COMMERCIAL_MEMO.md`.

## Project layout
```
lead_engine/        application code (config, models, db, pipeline, approval, delivery, metrics, app, cli)
fixtures/           generator + committed CSV/JSON/webhook synthetic data (102 records)
tests/              pytest suite (44 tests)
docs/               THREAT_MODEL.md, COMMERCIAL_MEMO.md
data/               runtime files (gitignored; regenerated on demand)
```

## Attribution
> The lines below describe how this repository was produced. Please verify the
> personal-effort / hours / cost lines against your own records before sending.

- **Built personally (design + implementation + review):** overall architecture,
  tenant isolation approach, the human-approval choke point, the strict AI
  safety policy, the retry/DLQ/reprocess design, all fixtures, tests, and docs,
  plus review/verification of every generated file.
- **AI-assisted:** an AI coding agent (Qoder) drafted much of the boilerplate
  code and documentation text, which was then reviewed and corrected. Two real
  defects were found and fixed during verification (a non-ASCII test symbol and
  a delivery/outbox ordering bug surfaced by the tests).
- **Hours spent:** _[fill in your actual figure]_
- **API / tooling cost:** $0 - the engine uses an offline mock LLM, no external
  AI calls, no paid services. The only cost is local development time.

## License / scope
Synthetic demo project for an evaluation task. Contains no real customer data,
credentials, or external integrations.
