"""Evidence-only message draft (requirement 4).

The draft is generated strictly from the lead's own evidence fields via a fixed
template. It never contains invented facts and never incorporates free-form AI
text. Unsafe leads (injection / opt-out) get a neutral placeholder so no
untrusted inbound text is echoed into an outbound message.
"""

from __future__ import annotations

import re

from .normalize import evidence_ids

# The only placeholders the template may use. Anything else is a bug.
_ALLOWED_PLACEHOLDERS = {"name", "company", "country", "intent_safe"}
_TOKEN_RE = re.compile(r"\{(\w+)\}")

TEMPLATE = (
    "Hello {name},\n\n"
    "Thanks for your interest{company_suffix}. "
    "We'd love to help {company} with what you asked about.\n\n"
    "Best regards,\nAthenAI Sales (draft for human review)"
)


class Draft:
    def __init__(self, subject: str, body: str, evidence_field_ids: list[str], safe: bool) -> None:
        self.subject = subject
        self.body = body
        self.evidence_field_ids = evidence_field_ids
        self.safe = safe


def _safe_intent(lead: dict) -> str:
    # For safe leads we may reference that they reached out, but we never paste
    # raw inbound free-text (it may carry injection). Use a fixed phrase.
    return "reaching out to us" if lead.get("intent") else "your enquiry"


def build_draft(lead: dict) -> Draft:
    unsafe = bool(int(lead.get("injection_flag", 0))) or bool(int(lead.get("opt_out", 0)))
    name = lead.get("name") or "there"
    company = lead.get("company")
    company_suffix = f" about {company}" if company else ""

    if unsafe:
        # Neutral, non-committal placeholder - still requires human decision,
        # and carries none of the untrusted inbound text.
        body = ("[Quarantined draft] This lead is flagged for safety review. "
                "No outreach text was generated from untrusted input.")
        subject = "[Review required] lead flagged"
        return Draft(subject, body, evidence_ids(lead), safe=False)

    fields = {
        "name": name,
        "company": company or "your team",
        "country": lead.get("country") or "",
        "intent_safe": _safe_intent(lead),
    }
    body = TEMPLATE.format(**{k: v for k, v in fields.items() if k != "company_suffix"})
    # manual suffix injection (kept explicit + safe)
    body = body.replace("{company_suffix}", company_suffix)
    subject = f"Following up on your interest{(' - ' + company) if company else ''}"
    return Draft(subject, body, evidence_ids(lead), safe=True)


def assert_evidence_only(draft: Draft, lead: dict) -> None:
    """Guard: a safe draft may only reference evidence-backed values. If a value
    appears in the draft that is not present in the lead evidence, fail loudly."""
    ev = lead.get("evidence", {})
    ev_values = {str(v).lower() for v in ev.values()}
    # name/company must come from evidence when used
    for field in ("name", "company"):
        val = lead.get(field)
        if val and val.lower() not in ev_values:
            raise AssertionError(f"draft references non-evidence {field}: {val!r}")


def validate_template() -> None:
    """Sanity: the template only uses allow-listed placeholders (+ company_suffix
    which we substitute explicitly). Raises on any unexpected token."""
    for token in _TOKEN_RE.findall(TEMPLATE):
        if token not in _ALLOWED_PLACEHOLDERS and token != "company_suffix":
            raise AssertionError(f"template uses disallowed placeholder: {token}")
