"""ps_service.persistence -- PS state Postgres connection and migration runner (issue #130).

Domain path: `ps.service.persistence`
(`docs/architecture/ps-solution-architecture.md`). Owns the shared PS state
Postgres connection helper, its connectivity probe, and the migration runner;
components (`ps_service.audit`, `ps_service.authz`) own their own tables and
migration directories and are wired to the runner by the composition root
(`ps_service.main`).
"""

from __future__ import annotations

from ps_service.persistence.connection import check_connectivity_from_config, connect_from_config
from ps_service.persistence.errors import (
    StatePostgresConnectionError,
    StatePostgresMigrationApplyError,
)
from ps_service.persistence.migration_runner import MigrationSource, apply_pending_migrations

__all__ = [
    "MigrationSource",
    "StatePostgresConnectionError",
    "StatePostgresMigrationApplyError",
    "apply_pending_migrations",
    "check_connectivity_from_config",
    "connect_from_config",
]
