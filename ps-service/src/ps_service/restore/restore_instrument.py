"""Restore of one curated instrument's artifact into the compliance graphs.

`restore_instrument` verifies a `RestoreArtifact` before doing anything else with it: the
checksum check, then the `schema_version` check -- both with zero graph calls, in that order,
so a corrupted or version-mismatched artifact is refused before the gateway is opened or any
graph is read.

Once verification passes:

1. Parse both blobs (`export.serialize.parse_serialized_graph_json`), content-validate both
   parsed graphs (`_validate_content`) and require an owner for governance content, still with
   zero graph calls.
2. Open the gateway (`open_gateway`; none installed fails the request closed) and build the
   native and baseline legs as upserts (`leg_primitives`).
3. Read `policy_system` at a known log position and build its leg
   (`merge_primitives.build_policy_system_leg`: the offline Company Merge as primitives over the
   live graph plus an overlay of the leg's own writes).
4. Submit one multi-graph group of the three legs under the restore's opening audit event: the
   native and baseline legs replace their graphs, the `policy_system` leg is guarded by
   `ExpectedPosition`. A refusal because `policy_system` moved (another writer) re-reads and
   rebuilds, up to `MAX_POLICY_SYSTEM_ATTEMPTS` times, then raises
   `RestoreConcurrencyConflictError`. Every other refusal raises a `RestoreGraphWriteError`.

An audit log entry is emitted at `"started"` (once verification has passed), `"succeeded"`
(after the group took effect) and `"failed"` (whenever anything after `"started"` raises -- no
`"succeeded"` entry follows a `"failed"` one).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.company_merge.falkordb_client import (
    select_graph as select_company_merge_graph,
)
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.serialize import checksum_bytes, parse_serialized_graph_json
from ps_service.graph_gateway.errors import StaleGraphStateError
from ps_service.graph_gateway.models import (
    ExpectedPosition,
    GraphLeg,
    MultiGraphGroup,
    MutationGroup,
)
from ps_service.logging.facade import emit_log_entry
from ps_service.restore import schema_allowlist
from ps_service.restore.errors import (
    ArtifactIntegrityError,
    ArtifactSchemaVersionMismatchError,
    RestoreConcurrencyConflictError,
    RestoreOwnerRequiredError,
)
from ps_service.restore.graph_submission import (
    MAX_POLICY_SYSTEM_ATTEMPTS,
    RestoreGateway,
    as_restore_gateway,
    ensure_caught_up,
    read_log_position,
    submit_restore,
)
from ps_service.restore.leg_primitives import embeddings_by_label, leg_primitives
from ps_service.restore.merge_primitives import build_policy_system_leg
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.export.models import SerializedGraph
    from ps_service.graph_gateway.models import Primitive
    from ps_service.graph_gateway.read_only import ReadOnlyDatabase
    from ps_service.logging.emitter import LogEmitter
    from ps_service.restore.models import RestoreArtifact

__all__ = ["restore_instrument"]

_COMPONENT = "restore"
_ACTION = "restore_instrument"
_RETRY_ACTION = "retry_policy_system_leg"


def _verify_checksums(artifact: RestoreArtifact) -> None:
    """Raise `ArtifactIntegrityError` if either blob's SHA-256 doesn't match its manifest digest.

    Checked before the `schema_version` comparison and before any graph call. Baseline is
    checked before native, matching `InstrumentManifest`'s own field order.
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

    Checked immediately after checksum verification, still before any graph call. There is no
    migrate or warn path: an exact string mismatch always refuses outright.
    """
    manifest_version = artifact.manifest.schema_version
    if manifest_version != DOMAIN_SCHEMA_VERSION:
        raise ArtifactSchemaVersionMismatchError(
            f"artifact {artifact.manifest.instrument_id!r} has schema_version "
            f"{manifest_version!r}, but this service requires schema_version "
            f"{DOMAIN_SCHEMA_VERSION!r}"
        )


def _validate_content(native_graph: SerializedGraph, baseline_graph: SerializedGraph) -> None:
    """Raise `ArtifactContentRejectedError` if EITHER parsed leg violates its allow-list.

    Both legs are content-validated here, before the gateway is opened or any graph is read,
    so a violation in either leg writes nothing. Native first, then baseline.
    """
    schema_allowlist.validate_serialized_graph(
        native_graph,
        allowed_labels=schema_allowlist.NATIVE_ALLOWED_LABELS,
        allowed_relationship_types=schema_allowlist.NATIVE_ALLOWED_RELATIONSHIP_TYPES,
    )
    schema_allowlist.validate_serialized_graph(
        baseline_graph,
        allowed_labels=schema_allowlist.BASELINE_ALLOWED_LABELS,
        allowed_relationship_types=schema_allowlist.BASELINE_ALLOWED_RELATIONSHIP_TYPES,
    )


def _emit_restore_log(
    *,
    instrument_id: str,
    outcome: str,
    actor: str,
    schema_version: str,
    emitter: LogEmitter | None,
    source: str | None = None,
    extra: dict[str, int] | None = None,
) -> None:
    """Emit one audit log entry for the restore of `instrument_id`.

    The entry's `extra` carries `"caller"` (the key the ingestion log entries use for the
    actor) and `"schema_version"`. The `"succeeded"` call folds in the PracticeArea, RiskPath
    and classification-edge write counts via `extra`, so the entry matches the live merge's
    `"succeeded"` entry; `"started"` and `"failed"` pass no `extra`, since no classification pass
    has necessarily run by then. A `"source"` key (the resolved curated-content source URL) is
    added only when `source` is not `None`, so an upload restore, which passes none, logs no
    `"source"`.

    Args:
        instrument_id: Canonical instrument id; becomes the entry's `entity_id`.
        outcome: `"started"`, `"succeeded"` or `"failed"`.
        actor: The restoring caller; logged as `extra["caller"]`.
        schema_version: The manifest's schema version; logged as `extra["schema_version"]`.
        emitter: Log emitter; `None` uses the default emitter.
        source: Resolved curated-content source URL, or `None` for an upload restore.
        extra: Additional counts merged into the entry's `extra`; `None` adds nothing.
    """
    payload: dict[str, object] = {"caller": actor, "schema_version": schema_version}
    if source is not None:
        payload["source"] = source
    payload.update(extra or {})
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        extra=payload,
        emitter=emitter,
    )


def _require_owner_for_governance(baseline_graph: SerializedGraph, owner: object | None) -> None:
    """Fail fast, before any graph is touched: a restored Policy needs an owner."""
    if owner is None and any(node.label == "Policy" for node in baseline_graph.nodes):
        raise RestoreOwnerRequiredError


def _restore_group(
    audit_event_id: str,
    legs: tuple[GraphLeg, ...],
) -> MultiGraphGroup | MutationGroup | None:
    """The one group a restore submits: its non-empty legs, or `None` when there are none.

    An empty leg cannot be a leg (a leg needs a primitive), so a leg with no content is left
    out. A restore of two or three non-empty legs is one multi-graph group. A single remaining
    leg (an artifact whose other legs are empty) is submitted as a plain group on its graph.
    """
    if len(legs) >= 2:  # noqa: PLR2004 -- a multi-graph group needs at least two legs
        return MultiGraphGroup(audit_event_id=audit_event_id, legs=legs, apply="swap")
    if legs:
        leg = legs[0]
        return MutationGroup(
            graph=leg.graph,
            audit_event_id=audit_event_id,
            primitives=leg.primitives,
            preconditions=leg.preconditions,
            checkpoint_requested=leg.checkpoint_requested,
        )
    return None


def _commit_restore(  # noqa: PLR0913 -- one restore's collaborators; no natural grouping
    gateway: RestoreGateway,
    *,
    live: GraphHandle,
    single_tenant_graph_name: str,
    baseline_graph: SerializedGraph,
    content_legs: tuple[GraphLeg, ...],
    instrument_id: str,
    governance: GovernanceImport,
    similarity_threshold: float,
    audit_event_id: str,
    emitter: LogEmitter | None,
) -> dict[str, int]:
    """Read `policy_system` at its log position, build its leg and submit the restore's group.

    The graph is made caught up with its log, its position read, and the leg built from reads of
    the live graph; the leg is guarded by that position, so a write landing in between makes the
    gateway refuse the whole group (nothing logged) and the next attempt reads again. After
    `MAX_POLICY_SYSTEM_ATTEMPTS` refused attempts `RestoreConcurrencyConflictError` is raised.

    Returns:
        The classification and governance counts of the built `policy_system` leg.
    """
    last_stale: StaleGraphStateError | None = None
    for attempt in range(1, MAX_POLICY_SYSTEM_ATTEMPTS + 1):
        ensure_caught_up(gateway, single_tenant_graph_name, emitter=emitter)
        position = read_log_position(gateway, single_tenant_graph_name)
        policy_system = build_policy_system_leg(
            baseline_graph,
            instrument_id=instrument_id,
            live=live,
            governance=governance,
            similarity_threshold=similarity_threshold,
            emitter=emitter,
        )
        legs = content_legs
        if policy_system.primitives:
            legs = (
                *legs,
                GraphLeg(
                    graph=single_tenant_graph_name,
                    primitives=policy_system.primitives,
                    preconditions=(ExpectedPosition(position=position),),
                    checkpoint_requested=True,
                ),
            )
        group = _restore_group(audit_event_id, legs)
        try:
            if group is not None:
                submit_restore(gateway, group)
        except StaleGraphStateError as exc:
            last_stale = exc
            emit_log_entry(
                component=_COMPONENT,
                action=_RETRY_ACTION,
                entity_id=instrument_id,
                outcome="stale",
                extra={"attempt": attempt, "max_attempts": MAX_POLICY_SYSTEM_ATTEMPTS},
                emitter=emitter,
            )
            continue
        return policy_system.counts
    raise RestoreConcurrencyConflictError(
        f"restore merge into {single_tenant_graph_name!r} aborted after "
        f"{MAX_POLICY_SYSTEM_ATTEMPTS} attempts due to a concurrent writer; "
        "no changes were made to the live graph"
    ) from last_stale


def _content_legs(
    short: str, native_graph: SerializedGraph, baseline_graph: SerializedGraph
) -> tuple[GraphLeg, ...]:
    """The native and baseline legs: each graph is replaced by its artifact's content."""
    legs: list[GraphLeg] = []
    for graph, serialized in (
        (f"{short}_native", native_graph),
        (f"{short}_baseline", baseline_graph),
    ):
        primitives: tuple[Primitive, ...] = leg_primitives(serialized)
        if primitives:
            legs.append(
                GraphLeg(
                    graph=graph, primitives=primitives, replace=True, checkpoint_requested=True
                )
            )
    return tuple(legs)


def restore_instrument(  # noqa: PLR0913 -- the restore's collaborators; no natural grouping
    artifact: RestoreArtifact,
    *,
    db: ReadOnlyDatabase,
    single_tenant_graph_name: str,
    similarity_threshold: float,
    actor: str,
    open_gateway: Callable[[], object],
    audit_event_id: str,
    emitter: LogEmitter | None = None,
    source: str | None = None,
    owner: tuple[str, str] | None = None,
) -> RestoreOutcome:
    """Restore one curated instrument's artifact end to end as one gateway group.

    Verification (checksum, then `schema_version`) runs first and unconditionally, with zero
    graph calls of any kind -- a rejected artifact never reaches `db`, the gateway or the audit
    log at all. Once verification passes, one `outcome="started"` audit log entry is emitted,
    then both blobs are parsed and content-validated (both legs, before the gateway is opened),
    the gateway is opened and the three legs are submitted as one multi-graph group (see the
    module docstring).

    On success, one `outcome="succeeded"` audit log entry is emitted and a `RestoreOutcome` is
    returned. On any exception raised after `"started"` one `outcome="failed"` entry is emitted
    and the exception is re-raised unchanged -- no `"succeeded"` entry ever follows a `"failed"`
    one.

    Business Rules: an internal-sourced artifact's Policy/Standard/Control tree is imported as
    `draft`, owned by `owner`, and never converged onto an existing Policy; an artifact with a
    Policy and no `owner` is refused before the gateway is opened.

    Args:
        artifact: The artifact to restore; its blobs are verified against its manifest.
        db: Read-only handle used only to read `policy_system`.
        single_tenant_graph_name: Name of the `policy_system` graph (non-empty).
        similarity_threshold: Minimum Capability embedding similarity for convergence.
        actor: The restoring caller; recorded as `caller` in the audit log entries.
        open_gateway: Returns the process's Graph Write Gateway, or raises when there is none;
            called after verification, parsing and content validation, before the first read of
            a live graph.
        audit_event_id: Id of the restore's opening `instrument.restore` audit row; every log
            group of the restore carries it.
        emitter: Log emitter; `None` uses the default emitter.
        source: Resolved curated-content source URL for the catalog restore, added to every
            audit log entry as `"source"`; `None` (upload restore) adds none.
        owner: The restoring caller's verified `(sub, iss)` pair; `None` when there is none.

    Returns:
        A `RestoreOutcome` with the instrument id and the stages that ran. A failed restore
        raises instead of returning.

    Side Effects:
        Emits `started`, then `succeeded` or `failed` audit log entries, plus a
        `retry_policy_system_leg` entry per stale attempt. Reads `policy_system` through `db`.
        On success the native and baseline graphs are replaced and `policy_system` is merged,
        all through the gateway as one logged group.

    Raises:
        ArtifactIntegrityError: a blob does not match its manifest digest.
        ArtifactSchemaVersionMismatchError: the manifest's schema_version is not this service's.
        ArtifactContentRejectedError: an artifact leg violates its allow-list or cannot be stored.
        RestoreOwnerRequiredError: governance content and no `owner`.
        RestoreConcurrencyConflictError: `policy_system` kept changing for every attempt.
        RestoreGroupRejectedError: the gateway refused the group; nothing was logged.
        RestoreGraphWriteFailedError: the log or FalkorDB failed; the restore did not complete.
        RestoreApplyPendingError: committed to the log, applied on recovery.
    """
    _verify_checksums(artifact)
    _verify_schema_version(artifact)

    manifest = artifact.manifest
    instrument_id = manifest.instrument_id
    # Lowercased to match the `{short_name.lower()}_native` / `{short_name.lower()}_baseline`
    # graph names that ingestion and the domain mapper write.
    short = manifest.short_name.lower()

    _emit_restore_log(
        instrument_id=instrument_id,
        outcome="started",
        actor=actor,
        schema_version=manifest.schema_version,
        emitter=emitter,
        source=source,
    )

    try:
        native_graph = parse_serialized_graph_json(artifact.native_blob)
        baseline_graph = parse_serialized_graph_json(artifact.baseline_blob)
        _validate_content(native_graph, baseline_graph)
        _require_owner_for_governance(baseline_graph, owner)
        content_legs = _content_legs(short, native_graph, baseline_graph)

        gateway = as_restore_gateway(open_gateway())
        classification_counts = _commit_restore(
            gateway,
            live=select_company_merge_graph(db, single_tenant_graph_name),
            single_tenant_graph_name=single_tenant_graph_name,
            baseline_graph=baseline_graph,
            content_legs=content_legs,
            instrument_id=instrument_id,
            governance=GovernanceImport(
                owner=owner, policy_embeddings=embeddings_by_label(baseline_graph, "Policy")
            ),
            similarity_threshold=similarity_threshold,
            audit_event_id=audit_event_id,
            emitter=emitter,
        )
    except Exception:
        _emit_restore_log(
            instrument_id=instrument_id,
            outcome="failed",
            actor=actor,
            schema_version=manifest.schema_version,
            emitter=emitter,
            source=source,
        )
        raise

    _emit_restore_log(
        instrument_id=instrument_id,
        outcome="succeeded",
        actor=actor,
        schema_version=manifest.schema_version,
        emitter=emitter,
        source=source,
        extra=classification_counts,
    )
    return RestoreOutcome(
        instrument_id=instrument_id,
        stages=("verified", "staged", "merged_and_finalized"),
    )
