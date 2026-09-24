"""Run provenance / reproducibility fingerprint.

Answers "exactly what produced these numbers?" in one glanceable block: the
git commit, interpreter + platform, the SHA-256 digest of every fixture, and a
snapshot of the resolved configuration knobs that move the funnel. Two runs
with the same provenance digest are, by construction, the same run - which is
what makes the golden-file test in ``tests/test_golden_demo.py`` meaningful.

Everything here is stdlib-only and degrades gracefully (e.g. if ``git`` is not
installed the commit is reported as ``unavailable`` rather than crashing).
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path


def _git_head(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            head = out.stdout.strip()
            # mark a dirty tree so the fingerprint changes if code was edited
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root), capture_output=True, text=True, timeout=5,
            )
            dirty = bool(status.stdout.strip())
            return f"{head}{'-dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001 - git is optional
        return "unavailable"
    return "unavailable"


def fixtures_digest(fixtures_dir: Path) -> str:
    """SHA-256 over sorted fixture files (name + bytes) - stable across runs."""
    h = hashlib.sha256()
    fx = Path(fixtures_dir)
    if not fx.exists():
        return "no-fixtures"
    for p in sorted(fx.glob("*.json")) + sorted(fx.glob("*.csv")):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:32]


def config_snapshot(settings) -> dict:
    return {
        "tenants": list(getattr(settings, "tenants", [])),
        "llm_provider": getattr(settings, "llm_provider", None),
        "ai_confidence_threshold": getattr(settings, "ai_confidence_threshold", None),
        "max_attempts": getattr(settings, "max_attempts", None),
        "crm_error_profile": getattr(settings, "crm_error_profile", None),
    }


def collect(settings, *, root: Path | None = None) -> dict:
    root = Path(root or Path(__file__).resolve().parents[1])
    prov = {
        "git_commit": _git_head(root),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "fixtures_digest": fixtures_digest(root / "fixtures"),
        "config": config_snapshot(settings),
    }
    # a single stable fingerprint over the whole provenance (excluding volatile
    # platform strings that legitimately differ across machines)
    fingerprint_src = "|".join([
        prov["git_commit"], prov["fixtures_digest"],
        str(prov["config"]["ai_confidence_threshold"]),
        str(prov["config"]["max_attempts"]),
        str(prov["config"]["crm_error_profile"]),
    ])
    prov["fingerprint"] = hashlib.sha256(fingerprint_src.encode()).hexdigest()[:16]
    return prov
