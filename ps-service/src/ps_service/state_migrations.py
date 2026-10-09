"""The one list of PS state Postgres migration sources (issue #205 follow-up).

Service startup (`ps_service.main`) and the operator provisioning command
(`python -m ps_service.graph_gateway.provision`) must apply the same ordinary component
migrations in the same order, so the list is declared once, here, at composition level:
`ps_service.persistence` deliberately never imports a component package.

`ORDINARY_STATE_MIGRATION_SOURCES` are applied by the ordinary runner (as the `ps_state`
application role). `GRAPH_GATEWAY_MIGRATION_SOURCE` is owner-protected and is applied only by
the privileged runner with the admin credential. `passkey_signing` is intentionally absent: it
lives in its own database.
"""

from __future__ import annotations

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.graph_gateway import MIGRATIONS_DIR as GRAPH_GATEWAY_MIGRATIONS_DIR
from ps_service.ingestion_runs import MIGRATIONS_DIR as INGESTION_RUNS_MIGRATIONS_DIR
from ps_service.persistence import MigrationSource
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR

ORDINARY_STATE_MIGRATION_SOURCES: tuple[MigrationSource, ...] = (
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
    MigrationSource("ingestion_runs", INGESTION_RUNS_MIGRATIONS_DIR),
)
"""Ordinary `ps_state` migrations, in apply order (audit first: graph_log links to it)."""

GRAPH_GATEWAY_MIGRATION_SOURCE = MigrationSource("graph_gateway", GRAPH_GATEWAY_MIGRATIONS_DIR)
"""The owner-protected graph log migration (privileged runner only)."""
