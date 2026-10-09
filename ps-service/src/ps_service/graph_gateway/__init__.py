"""ps_service.graph_gateway -- insert-only graph mutation log in the PS state Postgres (issue #205).

Domain path: `ps.service.graphgateway`
(`docs/architecture/ps-solution-architecture.md`). This package currently holds the store half
of the Graph Write Gateway: the owner-protected `graph_log` tables and the privileged
provisioning path that creates them. The gateway proper (submitting resolved mutations) is a
separate sub-issue.

Owns its migration directory (`MIGRATIONS_DIR`). Unlike every other component's migrations,
these are NOT applied by the ordinary startup runner (that would make the `ps_state` role their
owner and defeat the immutability protection): they are applied by the privileged runner,
`python -m ps_service.graph_gateway.provision`, with admin credentials.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""This component's migration directory, applied only by the privileged provisioning path."""

GRAPH_LOG_TABLES = (
    "graph_log.payloads",
    "graph_log.groups",
    "graph_log.entries",
    "graph_log.checkpoints",
    "graph_log.applied_markers",
)
"""Schema-qualified tables the `0001` migration creates; startup verifies each one."""

# Imported after `MIGRATIONS_DIR` is defined: the provisioning CLI imports it from this package.
from ps_service.graph_gateway.errors import (  # noqa: E402
    GraphLogPayloadError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
)
from ps_service.graph_gateway.models import (  # noqa: E402
    AppendedGroup,
    AppliedMarker,
    DigestCheckpoint,
    GraphLogEntry,
    GraphLogEntryDraft,
    GraphLogGroup,
    GraphLogGroupDraft,
)
from ps_service.graph_gateway.store import GraphLogStore, PsycopgGraphLogStore  # noqa: E402

__all__ = [
    "GRAPH_LOG_TABLES",
    "MIGRATIONS_DIR",
    "AppendedGroup",
    "AppliedMarker",
    "DigestCheckpoint",
    "GraphLogEntry",
    "GraphLogEntryDraft",
    "GraphLogGroup",
    "GraphLogGroupDraft",
    "GraphLogPayloadError",
    "GraphLogPersistenceError",
    "GraphLogStore",
    "GraphLogUnavailableError",
    "PsycopgGraphLogStore",
]
