"""ps_service.graph_gateway -- the Graph Write Gateway and its mutation log (issues #205-#208).

Domain path: `ps.service.graphgateway`
(`docs/architecture/ps-solution-architecture.md`). Two halves live here:

- The store (#205): the owner-protected, insert-only `graph_log` tables in the PS state Postgres
  and the privileged provisioning path that creates them
  (`python -m ps_service.graph_gateway.provision`).
- The gateway (#206): `GraphWriteGateway` takes a `MutationGroup` of resolved primitives,
  validates it (label allow-list, preconditions, no-op filter), appends it to the log (the
  commit point, log-first), applies the logged entries to FalkorDB with a last-applied marker,
  and reports a `GroupOutcome`. It also catches a graph up (`catch_up`, `is_caught_up`), recovers
  every lagging graph at startup (`recover`) and runs a background reconciler.
  `build_default_graph_write_gateway` composes it for the service. Replay and digest verification
  are #207; the ingestion pipelines (external and internal-seed) write only through it since
  #208, and the other writers move onto it in #209-#214.

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
from ps_service.graph_gateway.default_gateway import (  # noqa: E402
    build_default_graph_write_gateway,
)
from ps_service.graph_gateway.edge_identity_backfill import (  # noqa: E402
    EdgeIdentityBackfillOutcome,
    backfill_edge_identity,
)
from ps_service.graph_gateway.errors import (  # noqa: E402
    GraphApplyBlockedError,
    GraphApplyError,
    GraphDigestMismatchError,
    GraphLogCorruptEntryError,
    GraphLogGapError,
    GraphLogPayloadError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
    GraphReplayError,
    GraphReplayGatedError,
    GraphReplayStoppedError,
    GraphReplayUnverifiableError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    MissingTargetError,
    StaleGraphStateError,
    UnlistedNameError,
)
from ps_service.graph_gateway.gateway import GatewaySettings, GraphWriteGateway  # noqa: E402
from ps_service.graph_gateway.models import (  # noqa: E402
    AppendedGroup,
    AppliedMarker,
    CatchUpResult,
    DeleteEdge,
    DeleteNode,
    DigestCheckpoint,
    ExpectedPosition,
    GraphLogEntry,
    GraphLogEntryDraft,
    GraphLogGroup,
    GraphLogGroupDraft,
    GroupOutcome,
    MergeProperty,
    MutationGroup,
    NodeRef,
    RecoveryResult,
    RemoveProperty,
    ReplayReport,
    StampEdgeIdentity,
    StartupReplayReport,
    UpsertEdge,
    UpsertNode,
)
from ps_service.graph_gateway.staged_submission import StagedSubmission  # noqa: E402
from ps_service.graph_gateway.store import GraphLogStore, PsycopgGraphLogStore  # noqa: E402

__all__ = [
    "GRAPH_LOG_TABLES",
    "MIGRATIONS_DIR",
    "AppendedGroup",
    "AppliedMarker",
    "CatchUpResult",
    "DeleteEdge",
    "DeleteNode",
    "DigestCheckpoint",
    "EdgeIdentityBackfillOutcome",
    "ExpectedPosition",
    "GatewaySettings",
    "GraphApplyBlockedError",
    "GraphApplyError",
    "GraphDigestMismatchError",
    "GraphLogCorruptEntryError",
    "GraphLogEntry",
    "GraphLogEntryDraft",
    "GraphLogGapError",
    "GraphLogGroup",
    "GraphLogGroupDraft",
    "GraphLogPayloadError",
    "GraphLogPersistenceError",
    "GraphLogStore",
    "GraphLogUnavailableError",
    "GraphReplayError",
    "GraphReplayGatedError",
    "GraphReplayStoppedError",
    "GraphReplayUnverifiableError",
    "GraphUnavailableError",
    "GraphWriteGateway",
    "GraphWriteRejectedError",
    "GroupOutcome",
    "MergeProperty",
    "MissingTargetError",
    "MutationGroup",
    "NodeRef",
    "PsycopgGraphLogStore",
    "RecoveryResult",
    "RemoveProperty",
    "ReplayReport",
    "StagedSubmission",
    "StaleGraphStateError",
    "StampEdgeIdentity",
    "StartupReplayReport",
    "UnlistedNameError",
    "UpsertEdge",
    "UpsertNode",
    "backfill_edge_identity",
    "build_default_graph_write_gateway",
]
