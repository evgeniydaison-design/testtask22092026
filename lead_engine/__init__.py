"""AthenAI Lead Engine - a local, synthetic-only lead processing pipeline.

Package layout:
    config          runtime settings and tenant registry
    models          enums + strict pydantic schemas (AI / webhook)
    db              per-tenant scoped SQLite repository
    sources         CSV / JSON / webhook readers -> raw records
    pipeline        normalize -> dedup -> qualify -> draft -> orchestrate
    approval        human review gate (hard choke point)
    delivery        mock CRM + retry/DLQ/reprocess + local outbox
    metrics         funnel metrics
    app             FastAPI mock webhook / metrics endpoints
    cli             command line entry point
"""

__version__ = "1.0.0"
