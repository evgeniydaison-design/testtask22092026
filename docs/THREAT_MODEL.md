# Threat Model - AthenAI Lead Engine

Scope: a **local, synthetic-only** lead pipeline. There is no real CRM, no real
messaging, no internet egress, and no personal data. The threat model therefore
focuses on design-correctness risks that would matter if the same control plane
were wired to real systems.

## Assets
1. Tenant lead data (contacts, intent, decisions, evidence).
2. Tenant isolation boundary.
3. The outbound action (CRM write + outbox message) - the highest-impact lever.
4. Integrity of qualification (rules + AI) and the human approval record.

## Actors
- **Operator** (human reviewer) - trusted, makes approve/reject decisions.
- **Upstream sources** (CSV/JSON uploaders, webhook senders) - **untrusted**; may
  submit hostile or duplicated payloads.
- **AI/LLM provider** - semi-trusted; output treated as untrusted input.
- **Mock CRM / outbox** - the sink for outbound actions (local stand-ins).

## Threats, mitigations, residual risk

### T1. Cross-tenant data leakage (asset 1, 2)
- **Threat:** one tenant reads/mutates another tenant's leads.
- **Mitigation:** physical separation (one SQLite file per tenant for leads, CRM,
  and outbox) *plus* a repository bound to a `tenant_id` that filters every query
  and writes `tenant_id` on each row. The webhook validates the tenant id.
- **Tests:** `test_isolation_and_dedup.py` (not visible, not mutable, disjoint key
  sets).
- **Residual:** a single shared filesystem is trusted; in production these would
  be separate databases/credentials.

### T2. Prompt injection via lead content (asset 3, 4)
- **Threat:** a `record` carries "ignore previous instructions / reveal system
  prompt / you are now admin" to coerce auto-approval or to leak.
- **Mitigation:** `normalize.py` only *flags* such text (`injection_flag`) and
  never executes it. Flagged leads are forced to `manual_review`. The draft
  builder **quarantines** unsafe leads and does not echo inbound free-text. Only
  allow-listed evidence fields can appear in a message.
- **Tests:** `test_ai_cannot_auto_approve_injection_lead`,
  `test_draft_quarantines_injection_lead`, `test_normalize_flags_safety_cases`.
- **Residual:** injection phrase list is heuristic; defense-in-depth is that no
  automated path can deliver regardless.

### T3. Ungoverned AI output (asset 4)
- **Threat:** malformed/hallucinated/overscoped model output drives an action.
- **Mitigation:** output must parse into `AIQualification(extra="forbid")` with
  bounded confidence and evidence-only references. Unknown evidence ids,
  out-of-range confidence, non-JSON, or disagreement with an approve_ready rules
  verdict all force manual review. The model cannot reject-for-contact or
  auto-deliver; opt-out can never be approved.
- **Tests:** `test_ai_rejects_extra_field_schema`,
  `test_ai_rejects_out_of_range_confidence_and_bad_verdict`,
  `test_ai_non_json_output_forced_manual`,
  `test_ai_ungrounded_evidence_reference_is_flagged`, `test_ai_disagreement_forces_manual`.

### T4. Unauthorized outbound message / bypassing the human (asset 3)
- **Threat:** a draft reaches the CRM/outbox without a human decision.
- **Mitigation:** a single hard gate (`assert_deliverable`) requires stage
  `human_approved` **and** a recorded `approve` decision; the stage machine has no
  edge into `crm_synced`/`outboxed` except through `human_approved`; the outbox
  independently re-checks. Delivery and qualification are separate steps.
- **Tests:** `test_gate.py` (unapproved cannot deliver; outbox refuses; opt-out
  cannot be approved; illegal transitions), `test_end_to_end_never_autodelivers`.

### T5. Duplicate / replay abuse and double delivery (asset 1, 3)
- **Threat:** replayed webhooks create duplicate leads; retried deliveries create
  duplicate CRM rows/messages.
- **Mitigation:** inbound idempotency (`UNIQUE (tenant_id, idempotency_key)`);
  content-hash fallback; canonical-key dedup; outbound ops carry a stable
  idempotency key and the mock CRM short-circuits replays; the outbox is
  append-idempotent. `duplicate_of` + retained `lead_sources` preserve provenance.
- **Tests:** `test_inbound_idempotency_*`, `test_cross_source_dedup_preserves_all_evidence`,
  `test_idempotent_delivery_replay`, `test_reprocess_after_recovery_no_duplicates`.

### T6. Availability / partial failure (asset 3)
- **Threat:** CRM 429/5xx could drop or duplicate work.
- **Mitigation:** exponential backoff + jitter, bounded attempts, retryable vs
  non-retryable classification, DLQ + reprocess. Failures are observable via
  funnel metrics (`crm_status_histogram`, `delivery` states).
- **Tests:** `test_delivery_resilience.py`.

### T7. Secrets / egress
- **Threat:** real keys or external sends.
- **Mitigation:** offline mock LLM, no network in the pipeline, `.env.example`
  has no secrets and `.env` is gitignored, outbox is local JSONL flagged
  `MOCK_LOCAL_ONLY_NOT_SENT`.
- **Residual:** none within scope (no real integrations exist).

## Non-goals
Real deliverability, authN/authZ, encryption at rest, and abuse rate-limiting are
out of scope for this synthetic evaluation build and would be added before any
production wiring.
