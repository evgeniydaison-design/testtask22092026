"""Runtime configuration and tenant registry.

All configuration comes from environment variables with safe, local-only
defaults. The engine never talks to a real CRM and never sends real messages;
see ``.env.example``. No secrets are stored in the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    display_name: str
    owner: str


# Two strictly isolated tenants (synthetic identities only).
TENANTS: dict[str, Tenant] = {
    "tenant_alpha": Tenant("tenant_alpha", "Alpha Industries", "ops-alpha"),
    "tenant_beta": Tenant("tenant_beta", "Beta Logistics", "ops-beta"),
}


def get_tenant(tenant_id: str) -> Tenant:
    tenant = TENANTS.get(tenant_id)
    if tenant is None:
        raise KeyError(
            f"Unknown tenant {tenant_id!r}. Registered: {sorted(TENANTS)}"
        )
    return tenant


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    """Resolved settings. Read fresh via :func:`load_settings` so that tests can
    point ``ATHENAI_DATA_DIR`` at a temporary directory."""

    data_dir: Path
    tenants: tuple[str, ...] = field(default=("tenant_alpha", "tenant_beta"))
    llm_provider: str = "mock"
    ai_confidence_threshold: float = 0.75
    max_attempts: int = 5
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 30.0
    crm_error_profile: str = "none"
    host: str = "127.0.0.1"
    port: int = 8000

    # ---- storage paths (physical per-tenant isolation) --------------------
    def lead_store_path(self, tenant_id: str) -> Path:
        return self.data_dir / f"{tenant_id}.lead_store.db"

    def crm_store_path(self, tenant_id: str) -> Path:
        return self.data_dir / f"{tenant_id}.crm.db"

    def outbox_path(self, tenant_id: str) -> Path:
        return self.data_dir / f"{tenant_id}.outbox.jsonl"

    def audit_seal_path(self, tenant_id: str) -> Path:
        """Tamper-evident audit chain seal lives OUTSIDE the lead store so it can
        be copied to independent WORM storage (in prod it would go to a
        transparency log / signed external service, not this folder)."""
        return self.data_dir / f"{tenant_id}.audit.seal.json"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load_settings() -> Settings:
    data_dir = Path(_env("ATHENAI_DATA_DIR", "./data")).expanduser().resolve()
    tenants = tuple(
        t.strip() for t in _env("ATHENAI_TENANTS", "tenant_alpha,tenant_beta").split(",") if t.strip()
    )
    return Settings(
        data_dir=data_dir,
        tenants=tenants,
        llm_provider=_env("ATHENAI_LLM_PROVIDER", "mock"),
        ai_confidence_threshold=_env_float("ATHENAI_AI_CONFIDENCE_THRESHOLD", 0.75),
        max_attempts=_env_int("ATHENAI_MAX_ATTEMPTS", 5),
        base_backoff_seconds=_env_float("ATHENAI_BASE_BACKOFF_SECONDS", 0.5),
        max_backoff_seconds=_env_float("ATHENAI_MAX_BACKOFF_SECONDS", 30.0),
        crm_error_profile=_env("ATHENAI_CRM_ERROR_PROFILE", "none"),
        host=_env("ATHENAI_HOST", "127.0.0.1"),
        port=_env_int("ATHENAI_PORT", 8000),
    )
