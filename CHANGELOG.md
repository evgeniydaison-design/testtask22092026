# Changelog

All notable changes to **AthenAI Lead Engine** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version tags map directly to the git commits shown in the footer of each entry
so a reviewer can jump from a bullet here to the diff that produced it.

---

## [Unreleased]

### Added
- **Tamper-evident audit chain** (`lead_engine/audit_chain.py`): events +
  decisions folded into a per-tenant SHA-256 hash chain; head sealed to a file
  outside the lead store. New `cli audit-seal` and `cli verify-audit`
  (exit non-zero on `TAMPERED`). `cli demo` now seals automatically and prints
  the head hash. Covered by `tests/test_audit_chain.py` including two negative
  paths (rewrite a decision actor, delete an event) proving tamper detection.
  (wow pass)
- **Rules-vs-AI calibration** (`lead_engine/calibration.py`) + `cli calibrate`:
  disagreement matrix with both directions (`ai_more_lenient`, `ai_more_strict`,
  `safety_catches`) plus a monotonic **threshold what-if sweep** that recomputes
  the combined verdict across a ladder of `ai_confidence_threshold` values
  without any writes. Covered by `tests/test_calibration.py` (sweep equals the
  persisted verdicts at the current threshold; monotonicity proven). (wow pass)
- **Adversarial fuzzer** (`lead_engine/fuzz.py`) + `cli fuzz --n N --seed S`:
  deterministic generator (seeded RNG) produces a mix of benign, opt-out,
  injection, XSS, SQLi, fullwidth / mathematical-bold / zero-width obfuscated,
  anonymous and conflicting records, runs the real pipeline, and **actively
  attempts an unapproved outbox write on every lead**. Invariant: nothing
  reaches `human_approved` / `crm_synced` / `outboxed` / `dlq` and outbox stays
  empty. Covered by `tests/test_fuzz_invariants.py`. (wow pass)
- **One-file "explain this run"** (`lead_engine/run_report.py`) + `cli
  explain-run --out run.html`: a single self-contained HTML page combining
  funnel + **a state-machine SVG generated from `VALID_TRANSITIONS`** (so the
  diagram cannot drift from the code) + calibration + fuzz verdict + audit-chain
  status + provenance fingerprint. Covered by `tests/test_run_report.py`.
  (wow pass)
- **DSAR / data-subject export** — `lead_engine/audit.py::subject_export` and
  `cli subject-export --tenant <t> --email <e>` return everything held about one
  subject within one tenant; the cross-tenant negative case is a test. (wow pass)
- **Provenance fingerprint** (`lead_engine/provenance.py`) + `cli provenance`:
  git HEAD (with `-dirty` suffix), fixtures SHA-256 digest, config knobs,
  Python + platform, and a stable 16-hex combined `fingerprint`. (wow pass)
- New CLI subcommands: `audit-seal`, `verify-audit`, `calibrate`, `fuzz`,
  `subject-export`, `provenance`, `explain-run`. Total subcommands now **20**.
- 17 new tests (94 → **111**), covering every module added in this pass with
  both positive and negative paths. (wow pass)

### Changed
- `cli demo` now also seals the tenant's audit chain and prints the head +
  link count alongside the existing metrics — the golden test is unaffected
  because it asserts on the deterministic subset of the metrics dict.

_(previous entries from earlier passes below)_
- `cli audit` / `cli explain` — one-command full trail for a lead
  (raw sources → normalize flags → rules → AI → combined → decisions →
  delivery attempts + DLQ → outbox). (this pass)
- `cli report` — self-contained HTML dashboard across both tenants
  (`report.html`, opened in any browser, no external assets). (this pass)
- SLA metrics `avg_seconds_ingest_to_decision` and
  `avg_seconds_in_manual_review` in the funnel report. (this pass)
- `AdversarialMockLLMProvider` with seven explicit failure modes
  (`non_json`, `schema_extra`, `bad_confidence`, `ungrounded`,
  `empty_evidence`, `wrong_verdict`, `override_optout`) used to prove the
  safety wrapper end-to-end. (this pass)
- `fixtures/adversarial.json` — 15 hand-crafted malicious records
  (prompt injection, XSS, SQLi, unicode math-bold, fullwidth, zero-width,
  opt-out, opt-out-plus-injection, JSON-escape attempts) with expected
  flags recorded in `_meta`. (this pass)
- 50 new tests (44 → **94**), including:
  - parametrized adversarial-fixtures end-to-end (both "no delivery without
    approval" and "cannot reach outboxed" invariants);
  - adversarial LLM provider across all seven modes;
  - golden-file demo test pinning exact metrics for both tenants;
  - reproducibility test comparing two consecutive fresh demo runs.
- `docs/KNOWN_LIMITATIONS.md`-style notes integrated directly into README
  (regex-based guardrails, SQLite concurrency, English-only patterns).
- `load_test.py` script to run N synthetic leads through the pipeline and
  measure wall time / record throughput. (this pass)

### Changed
- `INJECTION_RE` extended to cover XSS (`<script`, `javascript:`, `on*=…=`)
  and SQLi (`DROP TABLE`, `UNION SELECT`, `;--`) markers, in addition to
  the existing LLM-jailbreak phrases.
- `_text_blob` now applies Unicode **NFKD** normalisation and strips
  zero-width / bidi control characters before pattern matching — this closes
  the common `𝐢𝐠𝐧𝐨𝐫𝐞 𝐩𝐫𝐞𝐯𝐢𝐨𝐮𝐬 instructions` and `ig​nore previous`
  obfuscations.
- `requirements.txt` now pins with upper bounds on the major
  (`pydantic>=2.7,<3`, etc.) and lists the exact verified working set at
  the bottom, so `pip install -r requirements.txt` remains reproducible.
- README: explicit **HTTP API security notice** (do not deploy on a public
  host), precise record-count methodology (raw source records vs unique
  leads), from-scratch reproduction one-liner (`rm -rf data/ && ...`),
  **Known limitations** section, and **hours / cost** filled in.

### Fixed
- Dead code: `db._NOW` and `crm_mock.flaky_keys`/`_flaky_remaining`
  (unused after the retry policy moved to `_call_count`-driven profiles).
- Two unused imports in the existing test suite.
- `_on_success` in `delivery/retry.py` previously advanced the lead stage to
  `crm_synced` *before* the guarded outbox write, which made the outbox
  raise on every delivery. Ordering fixed: outbox is written while the lead
  is still `human_approved`, then the stage advances to `crm_synced` →
  `outboxed`.

### Verified (this pass)
- `python -m pytest` → **94 passed** (Python 3.13.5, pytest 9.1.1).
- `python -m pyflakes lead_engine tests fixtures` → silent.
- `python -m lead_engine.cli demo` for both tenants reproduces the exact
  funnel pinned in `tests/test_golden_demo.py`.
- Webhook endpoint `POST /ingest/webhook/{tenant}` verified live via
  `TestClient`: 1st call `status=ingested`, 2nd call same payload
  `status=duplicate_ignored`, 3rd call same idempotency_key with different
  record `status=duplicate_ignored`, exactly one lead created end-to-end.
- CRM error profiles for both tenants:
  `flaky_429` → 25 succeeded / 0 dlq / `crm_hist={200:25, 429:50}`;
  `flaky_5xx` → 25 succeeded / 0 dlq / `{200:25, 503:25}`;
  `always_500` → 0 succeeded / 25 dlq / `{500:125}` (5 attempts × 25 leads);
  `non_retryable_405` → 0 succeeded / 25 dlq / `{405:25}`;
  `none` → 25 succeeded / 0 dlq / `{200:25}`.

---

## [0.1.0] — initial delivery

Scaffold + full lead engine + tests + docs.

### Added
- Per-tenant SQLite repository (`lead_engine/db.py`) with strictly scoped
  queries; two tenants `tenant_alpha` / `tenant_beta`.
- Models layer (`lead_engine/models.py`): stage state machine
  (`VALID_TRANSITIONS`, `can_transition`, `assert_transition`),
  `AIQualification` strict-schema pydantic model (`extra="forbid"`,
  `confidence ∈ [0, 1]`, non-empty `evidence_field_ids`).
- Pipeline: normalization (`normalize_email`, `normalize_phone`,
  `canonical_key`, opt-out / injection / conflict regexes, evidence build),
  idempotent ingest + dedup, rules-based qualification, safe AI adapter
  (MockLLMProvider + evidence grounding + safety forcing + verdict
  combination), evidence-only draft builder with quarantine.
- Approval gate: `approve_lead`, `reject_lead`, `edit_and_approve`,
  `pending_queue`, `review_view`, and the two-layer `assert_deliverable`
  (stage == human_approved AND an explicit approve decision row exists).
- Deep delivery module: `MockCRM` with five deterministic error profiles,
  guarded `MockOutbox`, `RetryPolicy` (exponential + jitter,
  `max_attempts=5`), retryable-vs-non-retryable classifier
  (429 / 5xx retry, 4xx straight to DLQ), `DeliveryService.process_op`
  / `process_all` / `reprocess_dlq`.
- Funnel metrics: sources / leads / duplicates / stage counts / verdict
  counts / ai_safety_flagged / manual_review_rate / delivery state counts
  / CRM status histogram / outbox entries.
- CLI (`lead_engine/cli.py`): `ingest`, `qualify`, `queue`, `review`,
  `approve`, `reject`, `deliver`, `reprocess`, `metrics`, `demo`.
- FastAPI app (`lead_engine/app.py`): `POST /ingest/webhook/{tenant}`,
  `GET /queue/{tenant}`, `GET /metrics/{tenant}`, `GET /health`.
- Fixtures (`fixtures/generate.py`): deterministic, 51 raw source records
  per tenant across CSV (24) + JSON (16) + webhook (11), producing 46
  unique leads per tenant after cross-source dedup.
- 44 pytest tests covering normalize / dedup / rules / AI / draft / gate /
  delivery resilience / DLQ reprocess / cross-tenant isolation / webhook
  idempotency / end-to-end demo.
- README with setup, run, requirement mapping table;
  `docs/THREAT_MODEL.md`; `docs/COMMERCIAL_MEMO.md`.

Commits:
`61b5091 scaffold`, `18a4227 pipeline`, `76b9fb4 approval+delivery`,
`4fb8f77 orchestrator+metrics+factory`, `087cc86 cli+http`,
`0fc1b60 fixtures+tests`, `a510e3b docs`.

[Unreleased]: #unreleased
[0.1.0]: #010--initial-delivery
