"""D8's staged-key lifecycle for `ps_service.restore`.

PLAN.md D8, CHANGES.md B1/MA4, CHANGES2.md §2/§3.6. Every write happens
against a *staged* copy; the target's real
`{short}_native`, `{short}_baseline`, and `policy_system` keys are never
touched until one final `RENAME`-based finalize step. `stage_graph`/
`snapshot_single_tenant` create staged copies (never touching the real
target); `finalize_atomic_swap` renames a batch of staged keys into place
inside one `MULTI`/`EXEC` block for callers with no concurrent writer
(native/baseline legs); `stage_and_finalize_policy_system_leg`
(CHANGES.md B1) is the `policy_system` leg's own WATCH-guarded optimistic-
concurrency retry loop, since that key -- unlike the per-instrument
native/baseline keys -- can be written by a concurrent live ingestion at
any time; `discard_staged_keys` cleans up abandoned staged keys on any
failure path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.export.falkordb_connection import graph_copy_handle, graph_query_handle
from ps_service.restore import populate, schema_allowlist
from ps_service.restore.errors import RestoreConcurrencyConflictError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from falkordb import FalkorDB

    # Cross-module import of this module-private Protocol is deliberate, per CHANGES.md
    # B1's own documented convention ("ps_service.restore imports both Protocols from
    # ps_service.export.falkordb_connection") -- the one-directional Export -> Restore
    # dependency already established by restore/models.py's InstrumentManifest import.
    from ps_service.export.falkordb_connection import (
        _RawGraphConnection,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import SerializedGraph

_MAX_POLICY_SYSTEM_MERGE_ATTEMPTS = 3
_EMPTY_GRAPH_OPERATION_MESSAGE = "Invalid graph operation on empty key"


@dataclass(frozen=True, slots=True)
class StagedLegNames:
    """One leg's staged key name and the target name it finalizes into.

    Groups `stage_and_finalize_policy_system_leg`'s native/baseline
    staged+target name pairs -- CHANGES.md B1's given signature passes
    these as four separate string parameters (nine total, including `db`/
    `connection`/`single_tenant_graph_name`/`token`/`run_offline_merge`),
    which trips this repo's `max-args = 8` rule (root `pyproject.toml`,
    matching L1's cyclomatic-complexity cap). The root `pyproject.toml`'s
    own comment on its two residual `PLR0913` cases records this project's
    preference for refactoring over suppressing that rule ("the 2 residual
    ... refactored"), so this dataclass -- not a `# noqa` -- is the
    deliberate, minimal adaptation: same information, seven parameters.
    """

    staged_name: str
    target_name: str


def stage_graph(
    db: FalkorDB,
    graph: SerializedGraph,
    key_name: str,
    *,
    allowed_labels: frozenset[str],
    allowed_relationship_types: frozenset[str],
) -> str:
    """Populate a fresh, uniquely-tokened staged key derived from `key_name` with `graph`'s content.

    Replaces D8 steps 2/3's old `stage_dump` (raw RESTORE), never touching
    `key_name` itself. Validates `graph` first (`schema_allowlist.
    validate_serialized_graph`) -- ordered after D9/D10's checksum/
    schema-version-first checks and before this staged key exists at all,
    so a rejected artifact never causes even a staged-key write.

    Signature change from the old `stage_dump(connection:
    _RawGraphConnection, blob: bytes, key_name: str)`: takes `db: FalkorDB`
    (needed for `db.select_graph`, since `populate_graph` issues
    GRAPH.QUERY, not a raw redis primitive) and a `SerializedGraph` instead
    of `bytes`, plus the two new allow-list parameters.
    """
    schema_allowlist.validate_serialized_graph(
        graph,
        allowed_labels=allowed_labels,
        allowed_relationship_types=allowed_relationship_types,
    )
    token = uuid.uuid4().hex
    staged_name = f"{key_name}__restoring__{token}"
    populate.populate_graph(graph_query_handle(db, staged_name), graph)
    if not graph.nodes and not graph.edges:
        # A genuinely empty artifact (D1: dump whatever is there -- zero
        # nodes/edges is a legitimate content) makes `populate_graph` issue
        # zero writes above, which would otherwise leave `staged_name`
        # never actually created (FalkorDB creates a graph key lazily, only
        # on its first command) -- the finalize step's later `RENAME` would
        # then fail with "no such key". Vivify it directly as a genuinely
        # empty graph, mirroring `snapshot_single_tenant`'s own MA4
        # empty-key vivify fix.
        graph_query_handle(db, staged_name).query("MATCH (n) WHERE false RETURN n")
    return staged_name


def snapshot_single_tenant(db: FalkorDB, single_tenant_graph_name: str, staged_name: str) -> str:
    """Snapshot `single_tenant_graph_name` into `staged_name` via `GRAPH.COPY`.

    MA4's fix: if `single_tenant_graph_name` has never been touched by any
    prior graph command (a genuinely fresh deployment), `GRAPH.COPY` raises
    `redis.exceptions.ResponseError('Invalid graph operation on empty
    key')` and creates no destination key (empirically confirmed live
    against a never-touched key, both source and destination `EXISTS ==
    0` afterward). Vivify `staged_name` directly as a genuinely empty graph
    instead of re-raising -- `resolve_capability_convergence_offline`'s
    existing_index is then naturally empty (PLAN.md §0.3's documented
    "first mint into an empty index" behavior), so D8 step 5 runs
    completely unchanged.
    """
    try:
        graph_copy_handle(db, single_tenant_graph_name).copy(staged_name)
    except redis.exceptions.ResponseError as exc:
        if _EMPTY_GRAPH_OPERATION_MESSAGE not in str(exc):
            raise
        graph_copy_handle(db, staged_name).query("MATCH (n) WHERE false RETURN n")
    return staged_name


def finalize_atomic_swap(
    connection: _RawGraphConnection, renames: Sequence[tuple[str, str]]
) -> None:
    """Rename every `(staged_name, target_name)` pair in one atomic `MULTI`/`EXEC` block.

    D8 step 7's finalize primitive for callers where nothing else writes
    the target keys concurrently (`{short}_native`/`{short}_baseline`
    legs) -- `RENAME`'s overwrite-destination semantics install the staged
    content whether or not `target_name` pre-existed. For the
    `policy_system` leg, which IS subject to concurrent writers, use
    `stage_and_finalize_policy_system_leg` instead (its own WATCH-guarded
    retry loop).
    """
    pipe = connection.pipeline(transaction=True)
    try:
        for staged_name, target_name in renames:
            pipe.rename(staged_name, target_name)
        pipe.execute()
    finally:
        pipe.reset()


def discard_staged_keys(connection: _RawGraphConnection, names: Sequence[str]) -> None:
    """Delete every name in `names` that exists; never raises for a missing one.

    Plain Redis `DELETE`, not `GRAPH.DELETE` -- deliberately: `GRAPH.DELETE`
    raises `ResponseError('Invalid graph operation on empty key')` for a
    key that was never created (confirmed live), while `DELETE` is
    natively idempotent (returns 0 for a missing key, never an error) --
    exactly what a partial-failure cleanup path needs.
    """
    connection.delete(*names)


def stage_and_finalize_policy_system_leg(
    db: FalkorDB,
    connection: _RawGraphConnection,
    single_tenant_graph_name: str,
    token: str,
    run_offline_merge: Callable[[str], None],
    native: StagedLegNames,
    baseline: StagedLegNames,
) -> None:
    """Merge into `single_tenant_graph_name` with WATCH-guarded optimistic concurrency.

    CHANGES.md B1's fix -- replaces D8 steps 4/5/7 for the `policy_system`
    leg only (native/baseline legs are staged once, outside this loop, and
    reused across every attempt). Each attempt: `WATCH` the live
    single-tenant graph, snapshot it (`snapshot_single_tenant`), run the
    caller-supplied offline merge into that snapshot, then attempt one
    `MULTI`/`EXEC` renaming all three staged names into place. A
    concurrent writer touching `single_tenant_graph_name` between `WATCH`
    and `EXEC` aborts the attempt with `redis.exceptions.WatchError`
    (empirically confirmed live, CHANGES.md B1); this function discards
    that attempt's snapshot and retries, up to
    `_MAX_POLICY_SYSTEM_MERGE_ATTEMPTS` times, before giving up and raising
    `RestoreConcurrencyConflictError` -- at which point every staged key
    (native, baseline, and the final snapshot) has already been discarded
    and the live graphs are untouched.
    """
    last_error: redis.exceptions.WatchError | None = None
    for attempt in range(1, _MAX_POLICY_SYSTEM_MERGE_ATTEMPTS + 1):
        snapshot_name = f"{single_tenant_graph_name}__restoring__{token}_{attempt}"
        pipe = connection.pipeline(transaction=True)
        try:
            pipe.watch(single_tenant_graph_name)
            snapshot_single_tenant(db, single_tenant_graph_name, snapshot_name)
            run_offline_merge(snapshot_name)
            pipe.multi()
            pipe.rename(native.staged_name, native.target_name)
            pipe.rename(baseline.staged_name, baseline.target_name)
            pipe.rename(snapshot_name, single_tenant_graph_name)
            pipe.execute()
        except redis.exceptions.WatchError as exc:
            last_error = exc
            discard_staged_keys(connection, (snapshot_name,))
            continue
        except Exception:
            discard_staged_keys(
                connection, (snapshot_name, native.staged_name, baseline.staged_name)
            )
            raise
        else:
            return
        finally:
            pipe.reset()
    discard_staged_keys(connection, (native.staged_name, baseline.staged_name))
    raise RestoreConcurrencyConflictError(
        f"restore merge into {single_tenant_graph_name!r} aborted after "
        f"{_MAX_POLICY_SYSTEM_MERGE_ATTEMPTS} attempts due to a concurrent writer; "
        "no changes were made to the live graph"
    ) from last_error
