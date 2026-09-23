"""Deterministic rules-based qualification (requirement 3).

Rules run first and are fully auditable. They assign a score from evidence
fields and produce an initial verdict:
  * reject          -> opt-out (never contact)
  * manual_review   -> injection / conflict / thin data
  * approve_ready   -> high score, complete data
  * manual_review   -> anything else
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Verdict

# Scoring weights (synthetic, tunable). Kept intentionally simple + readable.
BUDGET_SCORE = {"high": 3, "medium": 2, "low": 1}
INTENT_KEYWORDS = {
    "demo": 2, "price": 2, "pricing": 2, "buy": 3, "interested": 1,
    "quote": 2, "trial": 2, "info": 1, "partnership": 1,
}
TARGET_COUNTRIES = {"US", "GB", "DE", "FR", "NL", "SE", "CA", "AU"}


@dataclass
class RulesResult:
    verdict: str
    score: int
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "score": self.score, "reasons": self.reasons}


def score_lead(lead: dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    budget = (lead.get("budget_band") or "").lower()
    if budget in BUDGET_SCORE:
        score += BUDGET_SCORE[budget]
        reasons.append(f"budget_band={budget}+{BUDGET_SCORE[budget]}")

    intent = (lead.get("intent") or "").lower()
    hit = [k for k in INTENT_KEYWORDS if k in intent]
    if hit:
        add = max(INTENT_KEYWORDS[k] for k in hit)
        score += add
        reasons.append(f"intent:{','.join(hit)}+{add}")

    if (lead.get("country") or "").upper() in TARGET_COUNTRIES:
        score += 1
        reasons.append("country=target+1")

    if lead.get("email") and lead.get("phone"):
        score += 1
        reasons.append("contactable+1")

    return score, reasons


HIGH_SCORE = 4
MIN_COMPLETE_FIELDS = 4


def _completeness(lead: dict) -> int:
    return sum(1 for f in ("email", "phone", "name", "company", "country", "budget_band", "intent")
               if lead.get(f))


def qualify_rules(lead: dict) -> RulesResult:
    # Hard stops first - safety beats score.
    if int(lead.get("opt_out", 0)):
        return RulesResult(Verdict.REJECT.value, 0, ["opt_out=1 -> reject"])
    if int(lead.get("injection_flag", 0)):
        return RulesResult(Verdict.MANUAL_REVIEW.value, 0, ["prompt_injection detected -> manual"])
    if int(lead.get("conflict_flag", 0)):
        return RulesResult(Verdict.MANUAL_REVIEW.value, 0, ["conflicting contact data -> manual"])

    score, reasons = score_lead(lead)
    complete = _completeness(lead)

    if score >= HIGH_SCORE and complete >= MIN_COMPLETE_FIELDS:
        return RulesResult(Verdict.APPROVE_READY.value, score, reasons + [f"score>={HIGH_SCORE},complete>={MIN_COMPLETE_FIELDS}"])

    reasons.append(f"below threshold (score={score},complete={complete}) -> manual")
    return RulesResult(Verdict.MANUAL_REVIEW.value, score, reasons)
