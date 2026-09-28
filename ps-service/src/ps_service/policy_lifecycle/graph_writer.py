"""FalkorDB persistence for `ps_service.policy_lifecycle`'s single-tenant graph.

`find_existing_policy`/`create_policy_draft` (GH issue #134, S11): the two
Cypher-issuing halves of `ps_service.policy_lifecycle.service.create_policy_draft`
-- `find_existing_policy` is the read-side title-collision check
(AC-BI-022), `create_policy_draft` is the write-side Policy (+ optional
Standard/Control children) mint. Neither is wrapped in a translated
`PolicyLifecycleGraphUnavailableError` here -- per this module's own
"whichever slice first wraps a lifecycle call in a named error" note below,
that translation happens at `service.create_policy_draft`'s own call sites,
not in this module. A FalkorDB failure from either function still surfaces
through `redis.exceptions.RedisError`, unwrapped, exactly like
`backfill_governance_status`.

`backfill_governance_status` (GH issue #134, D-7/S8): the idempotent,
self-healing backfill for Standard/Control's new `status` property and
Policy's `version` property. No schema/data-migration mechanism exists in
this codebase (`docs/architecture/ps-solution-architecture.md`'s own
risk-register row), so this is implemented as a call-site-embedded,
`WHERE ... IS NULL`-guarded backfill rather than a standalone migration
script -- intended to be invoked at the top of every lifecycle read/cascade
call (S13+, not part of this slice) before any gate decision is made,
mirroring `ps_service.company_merge.graph_writer.backfill_canonical_embeddings`'s
own `WHERE n.embedding IS NULL` precedent (its own module docstring: "makes
a re-run's backfill call against an already-backfilled node a structural
no-op at the database-engine level").

Own copy of the `GraphHandle`/`GraphQueryResult` Protocols + `_execute_query`
dependency-health wrapper shape already vendored independently by
`ps_service.company_merge.graph_writer`, `ps_service.domain_mapper.graph_writer`,
and `ps_service.ingestion.adapters.internal_seed.persist` (L2's "fully
decoupled, no shared internal package between components" rule) -- a
deliberate near-duplicate, not a shared import.

**Cypher shape choice** (PLAN.md S8's own "implementer's choice" note between
a single `FOREACH`/`CASE`-guarded statement and two-or-more separate
`WHERE ... IS NULL` statements): this module uses **three** separate
statements (Policy `version`, Standard `status`, Control `status`) --
simpler to read, write, and test than one combined multi-clause statement,
and each one keeps the exact same single-purpose `WHERE ... IS NULL` shape
`backfill_canonical_embeddings` already established as this codebase's own
precedent for an idempotent backfill guard. The Control statement matches
the full `Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control`
path directly in one `MATCH`, so a Control's backfilled `status` always
comes from the root Policy's own `status` (D-7's literal requirement),
never a possibly-different intermediate Standard's status.

**No dedicated `policy_lifecycle` error type exists yet** (issue #134's S9
error taxonomy, including `PolicyLifecycleGraphUnavailableError`, is a
later, separate slice -- not part of this one). A FalkorDB failure here is
still recorded in `dependency_health` (for `/ready`'s live signal, mirroring
every other graph-writer module's connectivity-health wiring), but is
re-raised as the original `redis.exceptions.RedisError`, unwrapped, rather
than invented into a premature one-off domain exception. Whichever slice
first wraps a lifecycle call in a named error is expected to catch and
translate this at its own call site, the same way every other lifecycle
gate/cascade failure is translated (D-9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

__all__ = [
    "ControlDraft",
    "ControlRecord",
    "PolicyRecord",
    "StandardDraft",
    "StandardRecord",
    "backfill_governance_status",
    "cascade_status",
    "create_policy_draft",
    "find_approved_prior",
    "find_existing_policy",
    "read_policy_tree",
]


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    Every statement `backfill_governance_status` issues is a `SET`, never a
    `RETURN` -- this module never reads `result_set`, but the property is
    still declared so `GraphHandle.query`'s return type matches the real
    `falkordb.Graph.query` structurally.
    """

    @property
    def result_set(self) -> list[object]:
        """Result rows exactly as returned by `falkordb` (unused by this module)."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every write in this module goes through.

    Mirrors every other `graph_writer`-shaped module's own `_execute_query`:
    records FalkorDB connectivity failures in `dependency_health` for
    `/ready`'s live signal, self-healing on the next successful call.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise
    mark_healthy(FALKORDB)
    return result


def backfill_governance_status(graph: GraphHandle, policy_id: str) -> None:
    """Idempotently backfill `policy_id`'s own `version` and its tree's `status` (D-7).

    Three guarded writes, each a structural no-op on a re-run against
    already-backfilled data:

    1. `policy_id`'s own `version` -> `"1"`, only if currently `NULL`.
    2. Every `Standard` in `policy_id`'s `SUPPORTED_BY` tree whose `status`
       is `NULL` -> the Policy's own current `status`.
    3. Every `Control` in `policy_id`'s `SUPPORTED_BY`/`IMPLEMENTED_BY` tree
       whose `status` is `NULL` -> the Policy's own current `status` --
       always the ROOT Policy's `status`, never an intermediate Standard's,
       since the `MATCH` below walks the full two-hop path in one statement.

    A Standard/Control that already carries a non-`NULL` `status` is never
    touched, even if it differs from the Policy's current `status` -- the
    guard is a one-time-fill mechanism, not a continuous sync.
    """
    _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) SET p.version = coalesce(p.version, '1')",
        params={"policy_id": policy_id},
    )
    _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(s:Standard) "
        "WHERE s.status IS NULL "
        "SET s.status = p.status",
        params={"policy_id": policy_id},
    )
    _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(:Standard)"
        "-[:IMPLEMENTED_BY]->(c:Control) "
        "WHERE c.status IS NULL "
        "SET c.status = p.status",
        params={"policy_id": policy_id},
    )


def cascade_status(graph: GraphHandle, *, policy_id: str, target_status: str) -> None:
    """Set `policy_id`'s own `status`, plus every Standard/Control in its tree, to `target_status`.

    Issue #134, S15 -- the shared "cascade to a target status" write every
    lifecycle transition (`propose`/`approve`/`reject`/`revert`/
    `auto_deprecate`) needs, so S17/S19/S21/S25 call this directly rather
    than each re-deriving their own cascading Cypher (D-9's own shared-helper
    intent applies to the write half immediately -- unlike the
    audit-then-cascade *orchestration*, which D-9 explicitly defers
    extracting into `service.py` until Slice 24, this single Cypher `SET` has
    no per-action branching at all, so there is no "wrong abstraction" risk
    in sharing it from S15 onward).

    Deliberately **one** Cypher statement, not three separate `MATCH`/`SET`
    pairs like `backfill_governance_status` above: D-9's atomicity argument
    ("no partial cascade is left in the graph... FalkorDB executes one
    Cypher statement atomically") only holds if the whole tree is set in a
    single statement -- three separate statements could fail between the
    Policy write and the Standard/Control writes, leaving a partial cascade.
    The two `OPTIONAL MATCH`es let a Policy with zero Standards (or a
    Standard with zero Controls) still match and set the Policy's own
    `status` -- `SET` on a variable `OPTIONAL MATCH` left unbound (`null`) is
    a documented Cypher no-op, not an error, so the trailing `s`/`c` `SET`
    clauses are safe even when nothing matched them.

    Unlike `backfill_governance_status`'s `WHERE ... IS NULL` guard (a
    one-time fill), this always overwrites every `status`, unconditionally
    -- a lifecycle transition intentionally replaces whatever status was
    there before.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The root Policy id whose tree is being transitioned.
        target_status: The new `status` value for the Policy and its entire
            Standard/Control tree (e.g. `"proposed"`, `"approved"`,
            `"deprecated"`).
    """
    _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) "
        "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
        "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
        "SET p.status = $target_status, s.status = $target_status, "
        "c.status = $target_status",
        params={"policy_id": policy_id, "target_status": target_status},
    )


def find_approved_prior(graph: GraphHandle, successor_policy_id: str) -> str | None:
    """Find an `approved` Policy that `SUPERSEDED_BY`s `successor_policy_id` (D-10, S24).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every other call in this module -- no write, ever.

    No production code anywhere in issue #134 ever CREATES the
    `SUPERSEDED_BY` edge itself: that is #136's own fork tool
    (`supersedes_policy_id`), explicitly out of this issue's scope. This
    function only ever traverses an edge some other mechanism (a future
    #136 fork call, or a test fixture) already created -- `approve_policy`
    (S17/S25) calls this after its own successor-tree cascade succeeds, to
    decide whether an auto-deprecation cascade (AC-BI-019) is also needed.

    Args:
        graph: The single-tenant policy graph handle.
        successor_policy_id: The Policy id that was just (or is about to be)
            approved -- the `SUPERSEDED_BY` edge's target.

    Returns:
        The `id` of an inbound `SUPERSEDED_BY` Policy whose own `status` is
        currently `"approved"`, or `None` if no such edge/prior exists (an
        edge to a `draft`/`proposed` prior is deliberately not returned --
        only an already-`approved` prior is ever auto-deprecated).
    """
    result = _execute_query(
        graph,
        "MATCH (prior:Policy)-[:SUPERSEDED_BY]->(:Policy {id: $successor_policy_id}) "
        "WHERE prior.status = 'approved' "
        "RETURN prior.id LIMIT 1",
        params={"successor_policy_id": successor_policy_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    return cast("str", rows[0][0])


@dataclass(frozen=True, slots=True)
class ControlDraft:
    """One Control child to mint alongside a `create_policy_draft` call (D-6).

    `id` is already computed by the caller (`domain_mapper.identity.control_id`)
    -- this module issues Cypher only, it never derives identity itself.
    """

    id: str
    title: str
    control_type: str


@dataclass(frozen=True, slots=True)
class StandardDraft:
    """One Standard child (with its own optional Control children) to mint.

    `id` is already computed by the caller (`domain_mapper.identity.standard_id`),
    same convention as `ControlDraft.id`.
    """

    id: str
    title: str
    controls: tuple[ControlDraft, ...] = field(default_factory=tuple)


def find_existing_policy(graph: GraphHandle, policy_id: str) -> tuple[str, str] | None:
    """Read-check whether a Policy with `policy_id` already exists (AC-BI-022).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every write in this module -- a title-collision check must
    still count towards `/ready`'s FalkorDB connectivity signal.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The v1 `policy_id(title)` the caller is about to mint.

    Returns:
        `(existing_id, existing_title)` if a `Policy` node with this id
        already exists, else `None`.
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title",
        params={"policy_id": policy_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    existing_id, existing_title = rows[0]
    return cast("str", existing_id), cast("str", existing_title)


def create_policy_draft(
    graph: GraphHandle,
    *,
    policy_id: str,
    title: str,
    owner_subject: str,
    owner_issuer: str,
    standards: tuple[StandardDraft, ...] = (),
) -> None:
    """Mint a new draft Policy, plus any optional Standard/Control children (S11).

    The Policy node is written with `status="draft"`, `version="1"`, and the
    owner pair -- every optional Standard/Control child is written with
    `status="draft"` unconditionally (D-6), regardless of any status the
    caller might otherwise have implied. Each node is its own `MERGE`
    statement (mirrors `ingestion.adapters.internal_seed.persist`'s own
    per-node `MERGE (n:Label {id: $id}) SET n += $properties` shape) --
    this call site is only ever reached after `find_existing_policy` has
    already confirmed `policy_id` is unused, so `MERGE` here can never
    collide with a pre-existing node.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner_subject: The creating actor's `sub`.
        owner_issuer: The creating actor's `iss`.
        standards: Optional Standard children (each with its own optional
            Control children), ids already computed by the caller.
    """
    _execute_query(
        graph,
        "MERGE (p:Policy {id: $policy_id}) SET p += $properties",
        params={
            "policy_id": policy_id,
            "properties": {
                "title": title,
                "status": "draft",
                "version": "1",
                "owner_subject": owner_subject,
                "owner_issuer": owner_issuer,
            },
        },
    )
    for standard in standards:
        _execute_query(
            graph,
            "MATCH (p:Policy {id: $policy_id}) "
            "MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
            "SET s += $properties",
            params={
                "policy_id": policy_id,
                "standard_id": standard.id,
                "properties": {"title": standard.title, "status": "draft"},
            },
        )
        for control in standard.controls:
            _execute_query(
                graph,
                "MATCH (s:Standard {id: $standard_id}) "
                "MERGE (s)-[:IMPLEMENTED_BY]->(c:Control {id: $control_id}) "
                "SET c += $properties",
                params={
                    "standard_id": standard.id,
                    "control_id": control.id,
                    "properties": {
                        "title": control.title,
                        "type": control.control_type,
                        "status": "draft",
                    },
                },
            )


@dataclass(frozen=True, slots=True)
class ControlRecord:
    """One Control read back off an existing Policy's tree (issue #134, S13)."""

    id: str
    title: str
    status: str
    control_type: str


@dataclass(frozen=True, slots=True)
class StandardRecord:
    """One Standard (with its own Control children) read back off an existing Policy's tree."""

    id: str
    title: str
    status: str
    controls: tuple[ControlRecord, ...]


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """A Policy's own fields plus its full Standard/Control tree (issue #134, S13)."""

    id: str
    title: str
    status: str
    version: str
    owner_subject: str
    owner_issuer: str
    standards: tuple[StandardRecord, ...]


@dataclass
class _StandardAccumulator:
    """Mutable, per-Standard accumulator used only while grouping `read_policy_tree`'s rows."""

    title: str
    status: str
    controls: list[ControlRecord] = field(default_factory=list)


def read_policy_tree(graph: GraphHandle, policy_id: str) -> PolicyRecord | None:
    """Read `policy_id`'s own fields plus its full Standard/Control tree (S13).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every other call in this module. Callers are expected to call
    `backfill_governance_status` first (D-7) so `version`/`status` are never
    `NULL` by the time this read happens.

    The single Cypher statement below walks the full two-hop
    `Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control` path
    with `OPTIONAL MATCH`, so a Policy with zero Standards (or a Standard
    with zero Controls) still returns exactly one row for the Policy itself,
    with `NULL` Standard/Control columns -- grouped back into a nested
    `PolicyRecord` below.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The Policy id to read.

    Returns:
        A `PolicyRecord` with its `standards` (each with their own
        `controls`) in the order FalkorDB returned them, or `None` if no
        `Policy` node exists with this id.
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) "
        "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
        "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
        "RETURN p.id, p.title, p.status, p.version, p.owner_subject, p.owner_issuer, "
        "s.id, s.title, s.status, c.id, c.title, c.status, c.type",
        params={"policy_id": policy_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None

    first_row = rows[0]
    policy_id_value = cast("str", first_row[0])
    policy_title = cast("str", first_row[1])
    policy_status = cast("str", first_row[2])
    policy_version = cast("str", first_row[3])
    owner_subject = cast("str", first_row[4])
    owner_issuer = cast("str", first_row[5])

    standards_by_id: dict[str, _StandardAccumulator] = {}
    standard_order: list[str] = []
    for row in rows:
        (
            standard_id,
            standard_title,
            standard_status,
            control_id,
            control_title,
            control_status,
            control_type,
        ) = row[6:]
        if standard_id is None:
            continue
        standard_id = cast("str", standard_id)
        if standard_id not in standards_by_id:
            standards_by_id[standard_id] = _StandardAccumulator(
                title=cast("str", standard_title), status=cast("str", standard_status)
            )
            standard_order.append(standard_id)
        if control_id is not None:
            standards_by_id[standard_id].controls.append(
                ControlRecord(
                    id=cast("str", control_id),
                    title=cast("str", control_title),
                    status=cast("str", control_status),
                    control_type=cast("str", control_type),
                )
            )

    standards = tuple(
        StandardRecord(
            id=standard_id,
            title=standards_by_id[standard_id].title,
            status=standards_by_id[standard_id].status,
            controls=tuple(standards_by_id[standard_id].controls),
        )
        for standard_id in standard_order
    )
    return PolicyRecord(
        id=policy_id_value,
        title=policy_title,
        status=policy_status,
        version=policy_version,
        owner_subject=owner_subject,
        owner_issuer=owner_issuer,
        standards=standards,
    )
