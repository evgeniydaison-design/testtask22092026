"""Safe AI/LLM adapter (requirement 3).

Design goals:
  * The model output MUST parse into the strict ``AIQualification`` schema
    (extra="forbid", bounded confidence, non-empty grounded evidence ids).
  * The model may ONLY reference evidence ids that actually exist on the lead.
    Referencing an unknown field (possible hallucination / exfiltration probe)
    is a hard safety failure.
  * Any safety failure, low confidence, disagreement with an approve_ready
    rules verdict, or an injection-flagged lead is forced to MANUAL_REVIEW.
    The AI can never auto-reject or auto-approve an unsafe lead.

A real provider can be plugged into ``LLMProvider``; the default is an offline
deterministic mock so the whole system runs with no network and no keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from ..models import AIQualification, Verdict
from .normalize import evidence_ids


class LLMProvider(Protocol):
    def classify(self, prompt_payload: dict) -> str:
        """Return a raw JSON string conforming to AIQualification."""
        ...


@dataclass
class AIResult:
    verdict: str
    confidence: float
    grounded: bool
    schema_ok: bool
    safety_flags: list[str]

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "grounded": self.grounded,
            "schema_ok": self.schema_ok,
            "safety_flags": self.safety_flags,
        }


class MockLLMProvider:
    """Deterministic offline provider. Produces an approve_ready verdict for
    strong evidence, and supports adversarial outputs for tests via the special
    ``_force`` hook in the prompt payload (never used by real leads)."""

    def classify(self, payload: dict) -> str:
        forced = payload.get("_force")
        if isinstance(forced, str):
            return forced  # lets tests feed raw / malformed JSON
        ids = payload.get("evidence_field_ids", [])
        obj = {
            "verdict": "approve_ready" if len(ids) >= 4 else "manual_review",
            "confidence": 0.9 if len(ids) >= 4 else 0.5,
            "evidence_field_ids": ids or ["ev_name"],
            "reason": "evidence supports outreach" if len(ids) >= 4 else "insufficient evidence",
        }
        return json.dumps(obj)


def build_prompt_payload(lead: dict) -> dict:
    """Only evidence crosses the model boundary - never internal instructions."""
    ids = evidence_ids(lead)
    return {
        "task": "lead_qualification",
        "evidence_field_ids": ids,
        "evidence": lead.get("evidence", {}),
    }


def _fail_safety(flags: list[str], verdict: str = Verdict.MANUAL_REVIEW.value, confidence: float = 0.0) -> AIResult:
    return AIResult(verdict=verdict, confidence=confidence, grounded=False, schema_ok=False, safety_flags=flags)


def qualify_ai(lead: dict, provider: LLMProvider, *, ai_confidence_threshold: float = 0.75) -> AIResult:
    """Run the model + enforce the safety policy. Returns an AIResult whose
    verdict is one of approve_ready / manual_review / reject, but which is
    FORCED to manual_review on any safety concern."""
    payload = build_prompt_payload(lead)
    raw = provider.classify(payload)
    valid_ids = set(evidence_ids(lead))

    # 1. parse into strict schema
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _fail_safety(["non_json_output"])
    try:
        parsed = AIQualification.model_validate(data)
    except ValidationError as e:
        return _fail_safety([f"schema_violation:{e.errors()[0]['type'] if e.errors() else 'invalid'}"])

    flags: list[str] = []

    # 2. grounding: every referenced evidence id must exist
    unknown = [x for x in parsed.evidence_field_ids if x not in valid_ids]
    grounded = not unknown
    if unknown:
        flags.append(f"ungrounded_refs:{','.join(unknown)}")

    # 3. injection-flagged leads are never auto-approved by AI
    if int(lead.get("injection_flag", 0)):
        flags.append("lead_injection_flagged")

    # 4. opt-out leads: AI must not push toward contact
    if int(lead.get("opt_out", 0)) and parsed.verdict != Verdict.REJECT.value:
        flags.append("ai_did_not_reject_optout")

    # 5. low confidence -> manual
    low_conf = parsed.confidence < ai_confidence_threshold
    if low_conf:
        flags.append("low_confidence")

    verdict = parsed.verdict
    if flags:
        # any safety flag -> the AI cannot auto-approve; force human review.
        verdict = Verdict.MANUAL_REVIEW.value

    return AIResult(
        verdict=verdict,
        confidence=parsed.confidence,
        grounded=grounded,
        schema_ok=True,
        safety_flags=flags,
    )


def combine_verdicts(rules_verdict: str, ai: AIResult) -> str:
    """Fold rules + AI into the final pre-human verdict.

    Precedence: reject (opt-out) wins; then any safety/injection/conflict or a
    rules/AI disagreement -> manual_review; approve_ready only when rules AND a
    schema-valid, grounded, confident AI agree.
    """
    if rules_verdict == Verdict.REJECT.value:
        return Verdict.REJECT.value
    if ai.safety_flags:
        return Verdict.MANUAL_REVIEW.value
    if rules_verdict == Verdict.MANUAL_REVIEW.value:
        return Verdict.MANUAL_REVIEW.value
    # rules == approve_ready:
    if ai.verdict == Verdict.APPROVE_READY.value and ai.schema_ok and ai.grounded:
        return Verdict.APPROVE_READY.value
    # AI disagrees or is not confident/grounded -> a human decides.
    return Verdict.MANUAL_REVIEW.value
