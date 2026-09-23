"""Tenant service factory.

Assembles the repositories and delivery components for a single tenant so the
CLI, the FastAPI app and the tests all share one wiring path. Physical isolation:
each tenant gets its own lead store + CRM store + outbox file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, get_tenant, load_settings
from .db import LeadRepository
from .delivery.crm_mock import MockCRM
from .delivery.outbox import MockOutbox
from .delivery.retry import DeliveryService, RetryPolicy
from .pipeline.orchestrator import LeadPipeline
from .pipeline.qualify_ai import MockLLMProvider


@dataclass
class TenantService:
    tenant_id: str
    settings: Settings
    repo: LeadRepository
    crm: MockCRM
    outbox: MockOutbox
    pipeline: LeadPipeline
    delivery: DeliveryService

    def close(self) -> None:
        self.repo.close()
        self.crm.close()


def build_service(
    tenant_id: str,
    *,
    settings: Settings | None = None,
    crm_error_profile: str | None = None,
    sleep=None,
) -> TenantService:
    get_tenant(tenant_id)  # validates tenant exists
    settings = settings or load_settings()
    settings.ensure_data_dir()

    repo = LeadRepository(tenant_id, settings.lead_store_path(tenant_id))
    crm = MockCRM(
        settings.crm_store_path(tenant_id),
        error_profile=crm_error_profile or settings.crm_error_profile,
    )
    outbox = MockOutbox(settings.outbox_path(tenant_id))
    pipeline = LeadPipeline(repo, MockLLMProvider(), ai_threshold=settings.ai_confidence_threshold)
    policy = RetryPolicy(
        max_attempts=settings.max_attempts,
        base_seconds=settings.base_backoff_seconds,
        max_seconds=settings.max_backoff_seconds,
    )
    delivery = DeliveryService(repo, crm, outbox, policy=policy, sleep=sleep)
    return TenantService(
        tenant_id=tenant_id, settings=settings, repo=repo, crm=crm,
        outbox=outbox, pipeline=pipeline, delivery=delivery,
    )
