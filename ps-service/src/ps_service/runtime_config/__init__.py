"""ps_service.runtime_config -- runtime-mutable configuration store (issue #130).

Domain path: `ps.service.runtimeconfig`
(`docs/architecture/ps-solution-architecture.md`). Persists runtime-mutable config values
(the first is the curated-content source override, registered by `ps_service.curated_source`)
in the `runtime_config` table of the PS state Postgres. Each key is declared in a typed
registry (name, value type, validator); the store rejects unregistered keys and invalid values
before any write, and writes one `audit_events` row per `set`/`reset` in the same transaction
through `ps_service.audit`'s public `AuditStore` -- this component depends on `audit`
one-way and registers its own audit actions into it.

Owns its migration directory (`MIGRATIONS_DIR`), listed by the composition root
(`ps_service.main`) for the shared `ps_service.persistence` runner.
"""

from __future__ import annotations

from pathlib import Path

import ps_service.runtime_config.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import: registers this component's audit actions before any store call can reach AuditStore.record
from ps_service.runtime_config.errors import (
    RuntimeConfigError,
    RuntimeConfigInvalidValueError,
    RuntimeConfigPersistenceError,
    RuntimeConfigUnavailableError,
    RuntimeConfigUnknownKeyError,
)
from ps_service.runtime_config.registry import (
    AuditScalar,
    RuntimeConfigKey,
    define_runtime_config_key,
    prepare_runtime_config_value,
    register_runtime_config_key,
    require_runtime_config_key,
    resolve_runtime_config_key,
)
from ps_service.runtime_config.store import PsycopgRuntimeConfigStore, RuntimeConfigStore

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""This component's own migration directory, listed by the composition root (`ps_service.main`)."""

__all__ = [
    "MIGRATIONS_DIR",
    "AuditScalar",
    "PsycopgRuntimeConfigStore",
    "RuntimeConfigError",
    "RuntimeConfigInvalidValueError",
    "RuntimeConfigKey",
    "RuntimeConfigPersistenceError",
    "RuntimeConfigStore",
    "RuntimeConfigUnavailableError",
    "RuntimeConfigUnknownKeyError",
    "define_runtime_config_key",
    "prepare_runtime_config_value",
    "register_runtime_config_key",
    "require_runtime_config_key",
    "resolve_runtime_config_key",
]
