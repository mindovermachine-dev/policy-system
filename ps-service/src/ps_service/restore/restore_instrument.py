"""ps_service.restore.restore_instrument -- D8's restore orchestration entry point.

PLAN.md D5/D6/D8/D20, CHANGES.md B1/MA2, CHANGES2.md §3.7. `restore_instrument`
verifies a `RestoreArtifact` before doing anything else with it: D9's checksum
check, then D10's `schema_version` check -- both with zero FalkorDB calls (D8
step 1's "zero graph calls before this passes"), in that order, so a
corrupted or version-mismatched artifact is refused before any staged key is
ever created.

Once verification passes, the remainder of D8's staged-write sequence runs
(CHANGES2.md §3.7's parse step inserted between D10 and D8 step 2):

1. Parse both blobs (`export.serialize.parse_serialized_graph_json`) -- only
   now, after checksum/schema_version have already passed.
2. Stage the native blob (`staging.stage_graph`, schema-allow-listed via
   `schema_allowlist.NATIVE_ALLOWED_*`) into a fresh `{short}_native__
   restoring__{token}` key -- D20: no dedup step for this leg at all.
3. Stage the baseline blob likewise (`schema_allowlist.BASELINE_ALLOWED_*`).
4-7. `staging.stage_and_finalize_policy_system_leg` (CHANGES.md B1) runs D8
   steps 4 (`GRAPH.COPY` snapshot), 5 (the offline dedup merge, this
   module's own `_run_baseline_merge`, injected as `run_offline_merge`), and
   7 (the atomic three-way `RENAME` finalize) inside its own WATCH-guarded
   optimistic-concurrency retry loop -- discarding every staged key and
   re-raising on any non-`WatchError` exception (D8 step 6's all-or-nothing
   guarantee), or raising `RestoreConcurrencyConflictError` after exhausting
   its retries.

D14/AC-BI-016's audit log entry (MA2's exact call shape) is emitted at
`"started"` (once verification has passed), `"succeeded"` (after the whole
sequence above completes), and `"failed"` (whenever anything after
`"started"` raises -- no `"succeeded"` entry follows a `"failed"` one).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from ps_service.company_merge import graph_reader, graph_writer
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.falkordb_client import (
    select_graph as select_company_merge_graph,
)
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.falkordb_connection import raw_connection
from ps_service.export.serialize import checksum_bytes, parse_serialized_graph_json
from ps_service.logging.facade import emit_log_entry
from ps_service.restore import schema_allowlist
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreOutcome
from ps_service.restore.staging import (
    StagedLegNames,
    stage_and_finalize_policy_system_leg,
    stage_graph,
)

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.export.models import SerializedGraph
    from ps_service.logging.emitter import LogEmitter
    from ps_service.restore.models import RestoreArtifact

__all__ = ["restore_instrument"]

_COMPONENT = "restore"
_ACTION = "restore_instrument"


def _verify_checksums(artifact: RestoreArtifact) -> None:
    """Raise `ArtifactIntegrityError` if either blob's SHA-256 doesn't match its manifest digest.

    D9: checked before `schema_version` comparison and before any FalkorDB
    call of any kind. Baseline is checked before native, matching
    `InstrumentManifest`'s own field order.
    """
    manifest = artifact.manifest
    for blob_name, blob, expected in (
        ("baseline", artifact.baseline_blob, manifest.baseline_sha256),
        ("native", artifact.native_blob, manifest.native_sha256),
    ):
        actual = checksum_bytes(blob)
        if actual != expected:
            raise ArtifactIntegrityError(
                f"{blob_name} blob checksum mismatch for instrument "
                f"{manifest.instrument_id!r}: manifest declares {expected!r}, "
                f"computed {actual!r}"
            )


def _verify_schema_version(artifact: RestoreArtifact) -> None:
    """Raise `ArtifactSchemaVersionMismatchError` on a `schema_version` mismatch.

    D10: checked immediately after checksum verification, still before any
    FalkorDB call. No migrate/warn path -- an exact string mismatch always
    refuses outright.
    """
    manifest_version = artifact.manifest.schema_version
    if manifest_version != DOMAIN_SCHEMA_VERSION:
        raise ArtifactSchemaVersionMismatchError(
            f"artifact {artifact.manifest.instrument_id!r} has schema_version "
            f"{manifest_version!r}, but this service requires schema_version "
            f"{DOMAIN_SCHEMA_VERSION!r}"
        )


def _emit_restore_log(
    *, instrument_id: str, outcome: str, actor: str, schema_version: str, emitter: LogEmitter | None
) -> None:
    """Emit one D14/AC-BI-016 audit log entry, in MA2's corrected call shape.

    `extra={"caller": actor, "schema_version": ...}` -- `entity_id` mirrors
    `company_merge/merge.py`'s own `merge_baseline_graph` call
    (`entity_id=regulatory_instrument_id`); `extra`'s key name mirrors
    `api/ingestion_orchestration.py::_emit_run`'s own `"caller"` key exactly.
    Never `extra={"actor": ...}` -- MA2's explicit correction of D14's
    original wording, since `"caller"` is ingestion's real, existing key
    name.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        extra={"caller": actor, "schema_version": schema_version},
        emitter=emitter,
    )


def _capability_embeddings(baseline_graph: SerializedGraph) -> dict[str, tuple[float, ...]]:
    """Extract every Capability node's artifact-supplied embedding (D7), keyed by id.

    Read directly off the already-parsed baseline `SerializedGraph` --
    `graph_reader.read_baseline_graph`'s own `_CAPABILITY_QUERY` never reads
    `n.embedding` (a live baseline graph never carries one, PLAN.md §0.3), so
    this is the only place a restore ever recovers D7's artifact-supplied
    vectors. A Capability with no `embedding` property (never backfilled at
    export time) is simply absent from the returned mapping --
    `resolve_capability_convergence_offline`'s own `incoming_embeddings.get(
    node.id)` already treats a missing entry as "no artifact embedding."
    """
    embeddings: dict[str, tuple[float, ...]] = {}
    for node in baseline_graph.nodes:
        if node.label != "Capability":
            continue
        raw_embedding = node.properties.get("embedding")
        if raw_embedding is None:
            continue
        node_id = cast("str", node.properties["id"])
        embeddings[node_id] = tuple(cast("list[float]", raw_embedding))
    return embeddings


def _run_baseline_merge(
    db: FalkorDB,
    baseline_staged_name: str,
    regulatory_instrument_id: str,
    incoming_embeddings: dict[str, tuple[float, ...]],
    similarity_threshold: float,
    snapshot_name: str,
    emitter: LogEmitter | None,
) -> None:
    """D8 step 5 / D6: dedupe and merge the staged baseline graph into `snapshot_name`.

    Reads the already-staged baseline graph back via the EXISTING, unmodified
    `graph_reader.read_baseline_graph` (never reimplemented); dedupes its
    Capability nodes via `resolve_capability_convergence_offline` (Slices
    5.3/5.4, D6) against `snapshot_name`'s own existing canonical index; then
    persists via the EXISTING, unmodified `graph_writer.persist_*` functions
    (Slice 5.6's own requirement -- reused, not reimplemented), in the same
    write order `merge.py::merge_baseline_graph` itself uses: role/
    requirement passthrough, obligation passthrough, canonical Capability
    mints, rewired edges, then embedding backfill.

    This function is passed as `stage_and_finalize_policy_system_leg`'s
    `run_offline_merge` argument -- it writes only into `snapshot_name`
    (a staged `GRAPH.COPY` of the live single-tenant graph), never the live
    graph itself, and may run more than once across that function's own
    WATCH-guarded retry attempts.
    """
    baseline_staged_graph = select_company_merge_graph(db, baseline_staged_name)
    baseline = graph_reader.read_baseline_graph(baseline_staged_graph, regulatory_instrument_id)

    snapshot_graph = select_company_merge_graph(db, snapshot_name)
    dedup_result = resolve_capability_convergence_offline(
        baseline.capability_nodes,
        incoming_embeddings=incoming_embeddings,
        single_tenant_graph=snapshot_graph,
        threshold=similarity_threshold,
        emitter=emitter,
    )

    graph_writer.persist_role_and_requirement_passthrough(
        snapshot_graph,
        baseline.regulatory_instrument_id,
        baseline.regulatory_instrument_properties,
        baseline.role_nodes,
        baseline.requirement_nodes,
        baseline.provenance_edges,
    )
    graph_writer.persist_obligation_passthrough(snapshot_graph, baseline.obligation_nodes)
    graph_writer.persist_canonical_nodes(
        snapshot_graph, baseline.capability_nodes, dedup_result.resolutions, kind="Capability"
    )
    canonical_id_by_incoming_id = {
        resolution.incoming_id: resolution.canonical_id for resolution in dedup_result.resolutions
    }
    graph_writer.persist_rewired_edges(
        snapshot_graph, baseline.bare_edges, canonical_id_by_incoming_id
    )
    graph_writer.backfill_canonical_embeddings(
        snapshot_graph, kind="Capability", embeddings=dedup_result.embedding_backfills
    )


def restore_instrument(
    artifact: RestoreArtifact,
    *,
    db: FalkorDB,
    single_tenant_graph_name: str,
    similarity_threshold: float,
    actor: str,
    emitter: LogEmitter | None = None,
) -> RestoreOutcome:
    """Restore one curated instrument's artifact end to end (D8's full staged-write sequence).

    Verification (D9 checksum, D10 schema_version) runs first and
    unconditionally, with zero FalkorDB calls of any kind -- a rejected
    artifact never reaches `db`, `single_tenant_graph_name`, or the audit
    log at all. Once verification passes, one `outcome="started"` audit log
    entry is emitted, then both blobs are parsed and staged (D20: the native
    leg is a straight load, no dedup), then `staging.stage_and_finalize_
    policy_system_leg` (CHANGES.md B1) runs the baseline merge and the
    atomic three-way finalize under WATCH-guarded optimistic concurrency.

    On success, one `outcome="succeeded"` audit log entry is emitted and a
    `RestoreOutcome` is returned. On any exception raised after `"started"`
    (a merge-step failure, D8 step 6's discard-and-reraise, or `staging.
    stage_and_finalize_policy_system_leg` exhausting its retries and raising
    `RestoreConcurrencyConflictError`), one `outcome="failed"` audit log
    entry is emitted and the exception is re-raised unchanged -- no
    `"succeeded"` entry ever follows a `"failed"` one.
    """
    _verify_checksums(artifact)
    _verify_schema_version(artifact)

    manifest = artifact.manifest
    instrument_id = manifest.instrument_id
    short = manifest.short_name

    _emit_restore_log(
        instrument_id=instrument_id,
        outcome="started",
        actor=actor,
        schema_version=manifest.schema_version,
        emitter=emitter,
    )

    try:
        native_graph = parse_serialized_graph_json(artifact.native_blob)
        baseline_graph = parse_serialized_graph_json(artifact.baseline_blob)

        native_staged_name = stage_graph(
            db,
            native_graph,
            f"{short}_native",
            allowed_labels=schema_allowlist.NATIVE_ALLOWED_LABELS,
            allowed_relationship_types=schema_allowlist.NATIVE_ALLOWED_RELATIONSHIP_TYPES,
        )
        baseline_staged_name = stage_graph(
            db,
            baseline_graph,
            f"{short}_baseline",
            allowed_labels=schema_allowlist.BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=schema_allowlist.BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )

        incoming_embeddings = _capability_embeddings(baseline_graph)
        token = uuid.uuid4().hex

        def _run_offline_merge(snapshot_name: str) -> None:
            _run_baseline_merge(
                db,
                baseline_staged_name,
                instrument_id,
                incoming_embeddings,
                similarity_threshold,
                snapshot_name,
                emitter,
            )

        stage_and_finalize_policy_system_leg(
            db,
            raw_connection(db),
            single_tenant_graph_name,
            token,
            _run_offline_merge,
            StagedLegNames(staged_name=native_staged_name, target_name=f"{short}_native"),
            StagedLegNames(staged_name=baseline_staged_name, target_name=f"{short}_baseline"),
        )
    except Exception:
        _emit_restore_log(
            instrument_id=instrument_id,
            outcome="failed",
            actor=actor,
            schema_version=manifest.schema_version,
            emitter=emitter,
        )
        raise

    _emit_restore_log(
        instrument_id=instrument_id,
        outcome="succeeded",
        actor=actor,
        schema_version=manifest.schema_version,
        emitter=emitter,
    )
    return RestoreOutcome(
        instrument_id=instrument_id,
        stages=("verified", "staged", "merged_and_finalized"),
    )
