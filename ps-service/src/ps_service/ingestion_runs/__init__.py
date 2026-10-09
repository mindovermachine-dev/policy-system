"""ps_service.ingestion_runs -- status/result of asynchronously submitted ingestion runs (#194).

Domain path: `ps.service.ingestionruns`
(`docs/architecture/ps-solution-architecture.md`). Persists one `ingestion_runs` row per
`start_ingestion` MCP submission in the PS state Postgres, so a caller can poll a long
catalog-ingestion run (`get_ingestion_status`) instead of blocking on it. The `dispatch` module
holds the process-local registry of in-flight runs and launches each run's background thread.

Owns its migration directory (`MIGRATIONS_DIR`), listed by the composition root
(`ps_service.main`) for the shared `ps_service.persistence` runner.
"""

from __future__ import annotations

from pathlib import Path

from ps_service.ingestion_runs import (
    audit_actions,  # registers `ingestion_run.*` with `ps_service.audit` at import
)
from ps_service.ingestion_runs.errors import (
    IngestionRunInvalidCompletionError,
    IngestionRunPersistenceError,
    IngestionRunStoreError,
    IngestionRunStoreUnavailableError,
)
from ps_service.ingestion_runs.models import IngestionRunRow, IngestionRunStatus
from ps_service.ingestion_runs.store import IngestionRunStore, PsycopgIngestionRunStore

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""This component's own migration directory, listed by the composition root (`ps_service.main`)."""

__all__ = [
    "MIGRATIONS_DIR",
    "IngestionRunInvalidCompletionError",
    "IngestionRunPersistenceError",
    "IngestionRunRow",
    "IngestionRunStatus",
    "IngestionRunStore",
    "IngestionRunStoreError",
    "IngestionRunStoreUnavailableError",
    "PsycopgIngestionRunStore",
    "audit_actions",
]
