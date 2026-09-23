"""Lead normalization (requirement 2/3).

Turns an arbitrary raw record (from CSV / JSON / webhook) into a canonical
lead dict with:
  * normalized contact fields (email / phone / name / company / country ...)
  * derived safety flags: opt_out, injection, conflict, needs_review
  * a stable ``canonical_key`` used for dedup + idempotency
  * ``evidence`` - the list of field ids that actually carry data, which the AI
    adapter and draft builder are only ever allowed to reference.
"""

from __future__ import annotations

import hashlib
import json
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Explicit opt-out / do-not-contact phrases (requirement 3).
OPT_OUT_RE = re.compile(
    r"\b(stop|unsubscribe|do not contact|don't contact|no contact|remove me|opt[- ]?out)\b",
    re.IGNORECASE,
)

# Prompt-injection and hostile-text patterns that must never be executed, only
# flagged. Includes classic LLM jailbreak markers plus common XSS/SQLi payloads
# so that even a mistakenly-approved lead cannot smuggle them downstream.
INJECTION_RE = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions"
    r"|disregard (the )?(system|previous|above) (prompt|instructions)"
    r"|you are now"
    r"|system prompt"
    r"|new instructions:"
    r"|reveal (your|the) (prompt|system)"
    r"|override (the )?(rules|safety)"
    # XSS / markup smuggling (never rendered, but quarantine anyway)
    r"|<\s*script"
    r"|javascript\s*:"
    r"|on(error|load|click)\s*="
    # classic SQL-injection markers
    r"|drop\s+table"
    r"|union\s+select"
    r"|;\s*--)",
    re.IGNORECASE,
)

# Zero-width and bidi control characters used to break up keywords so a naive
# regex misses them (e.g. "ig\u200bnore previous instructions").
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]")

# Free-text fields we scan for opt-out / injection signals.
_TEXT_FIELDS = ("intent", "name", "company", "notes", "message", "raw_message")


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().lower()
    if "@" not in email:
        return None
    local, _, domain = email.partition("@")
    # gmail-style: strip dots and plus-tag in the local part
    if domain.endswith("gmail.com"):
        local = local.split("+", 1)[0].replace(".", "")
    else:
        local = local.split("+", 1)[0]
    email = f"{local}@{domain}"
    return email if EMAIL_RE.match(email) else None


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    # crude E.164-ish normalization: drop leading international 00 / single 0
    digits = digits.lstrip("0")
    return "+" + digits if len(digits) >= 7 else None


def _first(raw: dict, *keys: str) -> str | None:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def canonical_key(email: str | None, phone: str | None, name: str | None, company: str | None, raw: dict | None = None) -> str:
    """Stable identity key for dedup + idempotency (deterministic).

    Priority: normalized email -> normalized phone -> name+company hash ->
    full raw-record hash (anonymous records). The last case still yields a
    deterministic key so re-ingesting the same raw record never duplicates.
    """
    if email:
        return f"email:{email}"
    if phone:
        return f"phone:{phone}"
    base = f"{(name or '').lower()}|{(company or '').lower()}"
    if base.strip("|"):
        return "idhash:" + hashlib.sha256(base.encode()).hexdigest()[:16]
    seed = json.dumps(raw or {}, ensure_ascii=False, sort_keys=True)
    return "anon:" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def detect_conflict(raw: dict) -> bool:
    """A lead is 'conflicting' when two sources/fields disagree on contact."""
    emails = {normalize_email(e) for e in _as_list(raw.get("alt_emails"))}
    emails.discard(None)
    primary = normalize_email(raw.get("email"))
    if primary and emails and primary not in emails:
        return True
    # explicit conflict marker in data
    if str(raw.get("conflict", "")).lower() in ("1", "true", "yes"):
        return True
    return False


def _as_list(value) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [v.strip() for v in str(value).split(";") if v.strip()]


def _text_blob(raw: dict) -> str:
    # NFKD unifies unicode-obfuscated lookalikes (math-bold, fullwidth, etc.)
    # back to ASCII before the safety regexes run. Zero-width / bidi controls
    # are stripped first because they are used to break keywords apart.
    import unicodedata
    parts: list[str] = []
    for f in _TEXT_FIELDS:
        s = unicodedata.normalize("NFKD", str(raw.get(f, "")))
        parts.append(_INVISIBLE_RE.sub("", s))
    return " ".join(parts)


def normalize(raw: dict) -> dict:
    raw = dict(raw)
    email = normalize_email(_first(raw, "email", "e_mail", "email_address"))
    phone = normalize_phone(_first(raw, "phone", "mobile", "tel"))
    name = _first(raw, "name", "full_name", "contact")
    company = _first(raw, "company", "org", "organization")
    country = (_first(raw, "country") or "").strip().upper() or None
    budget_band = (_first(raw, "budget_band", "budget") or "").strip().lower() or None
    intent = _first(raw, "intent", "message", "notes", "raw_message")

    blob = _text_blob(raw)
    opt_out = bool(OPT_OUT_RE.search(blob)) or str(raw.get("opt_out", "")).lower() in ("1", "true", "yes")
    injection = bool(INJECTION_RE.search(blob))
    conflict = detect_conflict(raw)

    key = canonical_key(email, phone, name, company, raw)

    values = {"email": email, "phone": phone, "name": name, "company": company,
              "country": country, "budget_band": budget_band, "intent": intent}
    missing = [f for f, v in values.items() if not v]
    # needs_review when we cannot even identify the lead or data is thin
    needs_review = injection or conflict or (not email and not phone) or (not name)

    lead = {
        "canonical_key": key,
        "email": email,
        "phone": phone,
        "name": name,
        "company": company,
        "country": country,
        "budget_band": budget_band,
        "intent": intent,
        "opt_out": 1 if opt_out else 0,
        "needs_review": 1 if needs_review else 0,
        "injection_flag": 1 if injection else 0,
        "conflict_flag": 1 if conflict else 0,
        "missing_fields": missing,
    }
    lead["evidence"] = build_evidence(lead)
    return lead


# Evidence = the ground truth the AI and draft may use. Each field gets a stable
# id so the AI adapter can require grounding by referencing these ids only.
EVIDENCE_FIELDS = ("email", "phone", "name", "company", "country", "budget_band", "intent")


def build_evidence(lead: dict) -> dict[str, str]:
    return {f"ev_{i}": lead[i] for i in EVIDENCE_FIELDS if lead.get(i)}


def evidence_ids(lead: dict) -> list[str]:
    return list(build_evidence(lead).keys())
