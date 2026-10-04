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
from typing import TYPE_CHECKING, Protocol, cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ControlDraft",
    "ControlRecord",
    "ControlWithParent",
    "ForkGovernance",
    "ForkedControlRecord",
    "ForkedStandardRecord",
    "PolicyRecord",
    "StandardDraft",
    "StandardRecord",
    "StandardWithParent",
    "add_control_to_standard",
    "add_standard_to_policy",
    "approve_fork_repoint",
    "backfill_governance_status",
    "cascade_status",
    "create_policy_draft",
    "find_approved_prior",
    "find_control_with_parent",
    "find_existing_policy",
    "find_standard_with_parent",
    "read_capability_governors",
    "read_fork_governance",
    "read_policy_tree",
    "read_policy_tree_for_fork",
    "update_control_fields",
    "update_policy_fields",
    "update_standard_fields",
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
class ForkGovernance:
    """What a fork's superseded prior currently governs (issue #185).

    `capability_ids` may be empty: a legacy prior with no `GOVERNED_BY`
    edges (D-5) leaves nothing to move.
    """

    prior_id: str
    capability_ids: tuple[str, ...]


def read_fork_governance(graph: GraphHandle, policy_id: str) -> ForkGovernance | None:
    """Read the prior superseded by `policy_id` and the Capabilities it governs (issue #185).

    A pure read through the health-tracking wrapper. The prior's own status
    is deliberately not filtered (D-5: a non-approved prior is still
    approvable).

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The successor (fork) Policy id about to be approved.

    Returns:
        `None` when no inbound `SUPERSEDED_BY` edge exists, otherwise the
        prior's id and the ids of Capabilities it governs.
    """
    result = _execute_query(
        graph,
        "MATCH (prior:Policy)-[:SUPERSEDED_BY]->(:Policy {id: $policy_id}) "
        "OPTIONAL MATCH (cap:Capability)-[:GOVERNED_BY]->(prior) "
        "RETURN prior.id, collect(cap.id) LIMIT 1",
        params={"policy_id": policy_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    return ForkGovernance(
        prior_id=cast("str", rows[0][0]),
        capability_ids=tuple(cast("list[str]", rows[0][1])),
    )


def approve_fork_repoint(
    graph: GraphHandle,
    *,
    policy_id: str,
    prior_id: str,
    capability_ids: tuple[str, ...],
    target_status: str,
) -> bool:
    """Move `GOVERNED_BY` edges to `policy_id` and cascade its status, atomically (issue #185).

    Deliberately ONE Cypher statement: the guard
    `WHERE size(rels) = $expected` precedes every write, so if the governed
    set changed since `read_fork_governance` the statement returns no row
    and writes nothing. Correctness relies on that guard-before-write
    ordering, not on mid-statement rollback. Edge move and status cascade
    share the statement so a Capability never has zero or two governors.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The fork being approved (the new governor).
        prior_id: The superseded Policy that currently governs the Capabilities.
        capability_ids: The Capabilities read from `prior_id` beforehand.
        target_status: The status to cascade over the fork's tree.

    Returns:
        `True` when the move and cascade were applied, `False` when the
        guard failed (nothing was written).
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) "
        "OPTIONAL MATCH (cap:Capability)-[r:GOVERNED_BY]->(:Policy {id: $prior_id}) "
        "WHERE cap.id IN $capability_ids "
        "WITH p, collect(cap) AS caps, collect(r) AS rels WHERE size(rels) = $expected "
        "FOREACH (r IN rels | DELETE r) "
        "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p)) "
        "WITH p "
        "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
        "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c2:Control) "
        "SET p.status = $target_status, s.status = $target_status, "
        "c2.status = $target_status "
        "RETURN p.id",
        params={
            "policy_id": policy_id,
            "prior_id": prior_id,
            "capability_ids": list(capability_ids),
            "expected": len(capability_ids),
            "target_status": target_status,
        },
    )
    return bool(result.result_set)


@dataclass(frozen=True, slots=True)
class ControlDraft:
    """One Control child to mint alongside a `create_policy_draft` call (D-6).

    `id` is already computed by the caller (`domain_mapper.identity.control_id`)
    -- this module issues Cypher only, it never derives identity itself.

    `extra_properties` (issue #136, Slice 6): additional content fields to
    write alongside `title`/`type`/`status`, used by the supersede fork to
    carry a forked Control's full content across (`description`,
    `implementation_status`, etc.) -- every existing non-fork call site
    passes nothing, so `extra_properties` defaults to `{}` and this dataclass
    stays byte-for-byte backward compatible.
    """

    id: str
    title: str
    control_type: str
    extra_properties: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class StandardDraft:
    """One Standard child (with its own optional Control children) to mint.

    `id` is already computed by the caller (`domain_mapper.identity.standard_id`),
    same convention as `ControlDraft.id`.

    `extra_properties` (issue #136, Slice 6): same purpose as
    `ControlDraft.extra_properties`, one level up -- defaults to `{}`,
    preserving every existing non-fork call site unchanged.
    """

    id: str
    title: str
    controls: tuple[ControlDraft, ...] = field(default_factory=tuple)
    extra_properties: Mapping[str, object] = field(default_factory=dict[str, object])


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


def read_capability_governors(
    graph: GraphHandle, capability_ids: tuple[str, ...]
) -> dict[str, str | None]:
    """Read which Policy (if any) currently governs each of `capability_ids` (issue #185).

    A pure read through the health-tracking wrapper. A Capability that does
    not exist is absent from the result; an existing but ungoverned one maps
    to `None`.

    Args:
        graph: The single-tenant policy graph handle.
        capability_ids: The Capability ids a fresh draft wants to claim.

    Returns:
        `{capability_id: governing_policy_id | None}` for every id that exists.
    """
    result = _execute_query(
        graph,
        "MATCH (cap:Capability) WHERE cap.id IN $capability_ids "
        "OPTIONAL MATCH (cap)-[:GOVERNED_BY]->(g:Policy) "
        "RETURN cap.id, g.id",
        params={"capability_ids": list(capability_ids)},
    )
    governors: dict[str, str | None] = {}
    for row in cast("list[list[object]]", result.result_set):
        capability_id, governor_id = cast("str", row[0]), cast("str | None", row[1])
        if governors.get(capability_id) is None:
            governors[capability_id] = governor_id
    return governors


def create_policy_draft(
    graph: GraphHandle,
    *,
    policy_id: str,
    title: str,
    owner: tuple[str, str],
    standards: tuple[StandardDraft, ...] = (),
    supersedes_policy_id: str | None = None,
    version: str = "1",
    capability_ids: tuple[str, ...] = (),
) -> bool:
    """Mint a new draft Policy, plus any optional Standard/Control children (S11).

    The Policy node is written with `status="draft"`, `version=version`
    (defaults to `"1"`, the pre-#136 literal -- every existing non-fork call
    site keeps working byte-for-byte unchanged), and the owner pair -- every
    optional Standard/Control child is written with `status="draft"`
    unconditionally (D-6), regardless of any status the caller might
    otherwise have implied. Each node is its own `MERGE` statement (mirrors
    `ingestion.adapters.internal_seed.persist`'s own per-node
    `MERGE (n:Label {id: $id}) SET n += $properties` shape) -- this call site
    is only ever reached after `find_existing_policy` has already confirmed
    `policy_id` is unused, so `MERGE` here can never collide with a
    pre-existing node.

    `supersedes_policy_id` (issue #136, Slice 6): when not `None`, one
    additional statement links the superseded prior Policy to this new one
    via a single Policy-level `(prior)-[:SUPERSEDED_BY]->(new)` edge --
    AC-BI-005's "no per-Standard/Control lineage edges" requirement, so no
    other edge is ever written for the fork. Each Standard/Control child's
    own `extra_properties` (when the caller is forking, the source tree's
    full content minus `id`/`title`/`status`) is spread into that node's
    `SET` properties BEFORE `title`/`type`/`status` are set, so a stray
    copied `title`/`status`/`type` in `extra_properties` can never win --
    every non-fork call site passes `extra_properties={}` (the dataclass
    default), so this is a strict no-op there.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner: The creating actor's `(sub, iss)`.
        standards: Optional Standard children (each with its own optional
            Control children), ids already computed by the caller.
        supersedes_policy_id: The prior Policy id this new draft amends via
            `SUPERSEDED_BY`, or `None` for an ordinary (non-fork) draft.
        version: The new Policy's own `version` (string-typed, per
            `docs/artifacts`'s "version stays a string" decision) --
            `"1"` for an ordinary draft, or `str(int(prior_version) + 1)`
            for a fork (computed by the caller, `service.create_policy_draft`).
        capability_ids: Capabilities a fresh draft claims via `GOVERNED_BY`
            (issue #185). When non-empty, the Policy node and the edges are
            written by ONE guarded statement (the guard -- every id exists
            and is ungoverned -- precedes every write keyword), so a lost
            claim race writes nothing. The caller never passes these with
            `supersedes_policy_id`.

    Returns:
        `True` when the draft was written; `False` when the guarded claim
        statement matched nothing (a Capability was claimed concurrently) --
        in which case no Policy, edge, Standard or Control was written.
    """
    owner_subject, owner_issuer = owner
    properties = {
        "title": title,
        "status": "draft",
        "version": version,
        "owner_subject": owner_subject,
        "owner_issuer": owner_issuer,
    }
    if capability_ids:
        claimed = _execute_query(
            graph,
            "MATCH (cap:Capability) WHERE cap.id IN $capability_ids "
            "AND NOT (cap)-[:GOVERNED_BY]->(:Policy) "
            "WITH collect(cap) AS caps WHERE size(caps) = $expected "
            "MERGE (p:Policy {id: $policy_id}) SET p += $properties "
            "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p)) "
            "RETURN p.id",
            params={
                "capability_ids": list(capability_ids),
                "expected": len(capability_ids),
                "policy_id": policy_id,
                "properties": properties,
            },
        )
        if not claimed.result_set:
            return False
    else:
        _execute_query(
            graph,
            "MERGE (p:Policy {id: $policy_id}) SET p += $properties",
            params={"policy_id": policy_id, "properties": properties},
        )
    if supersedes_policy_id is not None:
        _execute_query(
            graph,
            "MATCH (prior:Policy {id: $prior_id}), (new:Policy {id: $new_id}) "
            "MERGE (prior)-[:SUPERSEDED_BY]->(new)",
            params={"prior_id": supersedes_policy_id, "new_id": policy_id},
        )
    for standard in standards:
        standard_properties: dict[str, object] = dict(standard.extra_properties)
        standard_properties["title"] = standard.title
        standard_properties["status"] = "draft"
        _execute_query(
            graph,
            "MATCH (p:Policy {id: $policy_id}) "
            "MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
            "SET s += $properties",
            params={
                "policy_id": policy_id,
                "standard_id": standard.id,
                "properties": standard_properties,
            },
        )
        for control in standard.controls:
            control_properties: dict[str, object] = dict(control.extra_properties)
            control_properties["title"] = control.title
            control_properties["type"] = control.control_type
            control_properties["status"] = "draft"
            _execute_query(
                graph,
                "MATCH (s:Standard {id: $standard_id}) "
                "MERGE (s)-[:IMPLEMENTED_BY]->(c:Control {id: $control_id}) "
                "SET c += $properties",
                params={
                    "standard_id": standard.id,
                    "control_id": control.id,
                    "properties": control_properties,
                },
            )
    return True


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


@dataclass(frozen=True, slots=True)
class ForkedControlRecord:
    """One Control's full content, read for a supersede fork (issue #136, Slice 6).

    Deliberately **not** `ControlRecord` (id/title/status/control_type only,
    G7) -- forking needs every content field (`description`,
    `implementation_status`, `execution_frequency`, ...), not just the
    narrow set `read_policy_tree` returns for `get-policy`'s own view.
    `properties` is the Control node's full property map as FalkorDB's own
    `properties()` function returns it, including `id`/`title`/`status`/
    `type` -- the caller (`service._build_forked_standard_drafts`) strips
    those four keys back out before reusing the rest as a new Control's
    `extra_properties`, since a forked node always gets its own freshly
    computed id/status and `title`/`type` are set from this record's own
    dedicated `title` field / the caller's own `control_type`.
    """

    title: str
    properties: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ForkedStandardRecord:
    """One Standard's full content (plus its Control children), read for a supersede fork.

    Same rationale as `ForkedControlRecord`, one level up.
    """

    title: str
    properties: Mapping[str, object]
    controls: tuple[ForkedControlRecord, ...]


@dataclass
class _ForkedStandardAccumulator:
    """Mutable, per-Standard accumulator for grouping `read_policy_tree_for_fork`'s own rows."""

    title: str
    properties: Mapping[str, object]
    controls: list[ForkedControlRecord] = field(default_factory=list)


def read_policy_tree_for_fork(
    graph: GraphHandle, policy_id: str
) -> tuple[ForkedStandardRecord, ...]:
    """Full-content read of `policy_id`'s Standard/Control tree, for a supersede fork (Slice 6).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every other call in this module. Deliberately separate from
    `read_policy_tree` (G7): that function's `StandardRecord`/`ControlRecord`
    only carry `id`/`title`/`status`(+`control_type`) -- enough for
    `get-policy`'s own view, not enough to fork real content (`description`,
    `procedure`, `execution_frequency`, etc.) into a new draft. This
    function's single Cypher statement walks the same
    `Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control` path
    with `OPTIONAL MATCH` (mirrors `read_policy_tree`'s own shape), but
    returns each node's full `properties()` map instead of a handful of
    named columns -- `s.id`/`c.id` are returned alongside each map purely to
    key the Python-side grouping-by-Standard accumulator on a stable scalar
    (FalkorDB's own `properties()` map already includes `id` as an ordinary
    property, per this schema's `MERGE (n:Label {id: $id})` convention, so
    the separate `s.id`/`c.id` columns are redundant with the map's own `id`
    key -- kept anyway for an unambiguous, never-`None`-when-a-node-matched
    grouping key, matching `read_policy_tree`'s own `standard_id`/
    `control_id`-column convention).

    Callers are expected to have already confirmed `policy_id` is
    `"approved"` (AC-BI-011) before calling this -- this function itself
    does not gate on status, it only reads.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The Policy id whose current Standard/Control tree is
            being forked.

    Returns:
        Every Standard under `policy_id` (each with its own Control
        children), in the order FalkorDB returned them. A Policy with zero
        Standards returns `()`.
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(s:Standard) "
        "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
        "RETURN s.id, properties(s), c.id, properties(c)",
        params={"policy_id": policy_id},
    )
    rows = cast("list[list[object]]", result.result_set)

    standards_by_id: dict[str, _ForkedStandardAccumulator] = {}
    standard_order: list[str] = []
    for row in rows:
        standard_id_value, standard_properties, control_id_value, control_properties = row
        if standard_id_value is None:
            continue
        standard_id_value = cast("str", standard_id_value)
        standard_properties = cast("Mapping[str, object]", standard_properties)
        if standard_id_value not in standards_by_id:
            standards_by_id[standard_id_value] = _ForkedStandardAccumulator(
                title=cast("str", standard_properties.get("title", "")),
                properties=standard_properties,
            )
            standard_order.append(standard_id_value)
        if control_id_value is not None:
            control_properties = cast("Mapping[str, object]", control_properties)
            standards_by_id[standard_id_value].controls.append(
                ForkedControlRecord(
                    title=cast("str", control_properties.get("title", "")),
                    properties=control_properties,
                )
            )

    return tuple(
        ForkedStandardRecord(
            title=standards_by_id[standard_id_value].title,
            properties=standards_by_id[standard_id_value].properties,
            controls=tuple(standards_by_id[standard_id_value].controls),
        )
        for standard_id_value in standard_order
    )


def update_policy_fields(
    graph: GraphHandle, *, policy_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Policy's own content fields (issue #136, Slice 1, AC-BI-008).

    An explicit `None` value in `properties` means "clear this field to
    null" (a legitimate PATCH semantic, distinct from the key being absent
    entirely, which leaves the existing value untouched -- the caller
    already only includes keys it means to change). No call site anywhere
    in this codebase's `graph_writer.py`-shaped modules relies on FalkorDB's
    `SET n += $map` map-merge operator clearing a property via an explicit
    `null` value in the map (CHANGES.md finding #6) -- that behavior is not
    documented as delete-on-null by the underlying Cypher/openCypher
    semantics this operator follows, unlike a plain `SET n.prop = null`,
    which unambiguously clears one property. This function therefore splits
    `properties` into two groups and issues one statement per group: a
    single `SET p += $set_properties` for every non-`None` value, then one
    separate `SET p.<key> = null` per explicitly-`None` key.

    Property KEY NAMES are never caller-controlled strings here: `properties`
    is only ever `mcp_interface.mcp_server._parse_patch_fields`'s already-
    validated output, whose keys are drawn exclusively from a fixed,
    code-defined allow-list (`service._POLICY_PATCHABLE_FIELDS`) -- so
    f-string-interpolating a key into the null-clearing Cypher text below
    (required because FalkorDB/openCypher does not support parameterized
    property names) is exactly as safe as `ingestion.adapters.internal_seed
    .persist`'s own `f"MERGE (n:{node.label} ...)"` dynamic-label
    precedent, never raw user input. This module issues Cypher only, it
    does not re-validate `properties`' keys itself (the MCP-boundary parser
    already did) -- matching this module's own established division of
    labor.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The existing Policy id to patch.
        properties: Field name -> new value (or `None` to clear), already
            restricted to the allowed patchable-field set by the caller.
    """
    set_properties = {key: value for key, value in properties.items() if value is not None}
    null_keys = [key for key, value in properties.items() if value is None]
    if set_properties:
        _execute_query(
            graph,
            "MATCH (p:Policy {id: $policy_id}) SET p += $set_properties",
            params={"policy_id": policy_id, "set_properties": set_properties},
        )
    for key in null_keys:
        _execute_query(
            graph,
            f"MATCH (p:Policy {{id: $policy_id}}) SET p.{key} = null",
            params={"policy_id": policy_id},
        )


def add_standard_to_policy(
    graph: GraphHandle,
    *,
    policy_id: str,
    standard_id: str,
    title: str,
    extra_properties: Mapping[str, object],
) -> None:
    """Mint a new Standard under an existing draft Policy (issue #136, Slice 2, AC-BI-007).

    Unlike `update_policy_fields` (Slice 1), a newly-minted node has no prior
    value to clear -- an explicit `None` in `extra_properties` (a caller
    passing `fields={"description": None}` to `add-standard-to-draft`, say)
    simply means "don't set this field at all", never a `SET s.<key> = null`
    statement (CHANGES.md finding #6's own carve-out for add/create
    functions, distinct from every PATCH function's own null-clearing
    split). `None`-valued keys are filtered out before the merge.

    `status` (governance status) is always forced to `"draft"`,
    unconditionally, regardless of anything in `extra_properties` --
    mirrors `create_policy_draft`'s own D-6 discipline. `implementation_status`
    defaults to `"draft"` (AC-BI-007) unless the caller already supplied it
    in `extra_properties` (already validated against
    `service._STANDARD_IMPLEMENTATION_STATUS_VALUES` by the MCP-boundary
    parser). Both `title` and `status` are set AFTER `extra_properties`'s own
    values, so neither key -- already excluded from the MCP-boundary
    allow-list anyway -- could ever be overridden by a caller-supplied value;
    defence in depth, matching this module's own established convention.

    Property KEY NAMES are never caller-controlled strings here -- this
    function issues one fully-parameterized statement, no f-string
    key-interpolation is needed (unlike `update_policy_fields`'s own
    null-clearing branch).

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The existing draft Policy id to attach this Standard to.
        standard_id: The already-computed new Standard id
            (`domain_mapper.identity.standard_id(policy_id, title)`).
        title: The new Standard's title.
        extra_properties: Any additional patchable content fields the
            caller supplied at creation time (already restricted to
            `service._STANDARD_PATCHABLE_FIELDS` by the MCP-boundary
            parser) -- `None`-valued keys are dropped, never written.
    """
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "draft")
    properties["title"] = title
    properties["status"] = "draft"
    _execute_query(
        graph,
        "MATCH (p:Policy {id: $policy_id}) "
        "MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
        "SET s += $properties",
        params={"policy_id": policy_id, "standard_id": standard_id, "properties": properties},
    )


@dataclass(frozen=True, slots=True)
class StandardWithParent:
    """A Standard's own status/title plus its parent Policy's owner/status (issue #136, Slice 3).

    TASK.md's Implementation-decisions paragraph: Standard has no
    `owner_subject`/`owner_issuer` property of its own -- ownership is
    derived by a one-hop query-time graph traversal to the parent Policy,
    never a stored/denormalized field on Standard itself. This row is that
    traversal's full result, letting the service layer build a
    `PolicyLifecycleRuleContext` (owner pair + the STANDARD's own status,
    the node directly being mutated -- never the parent Policy's status)
    without a second read.
    """

    policy_id: str
    policy_owner_subject: str
    policy_owner_issuer: str
    policy_status: str
    standard_status: str
    standard_title: str


def find_standard_with_parent(graph: GraphHandle, standard_id: str) -> StandardWithParent | None:
    """One-hop transitive-ownership read for an existing Standard (issue #136, Slice 3).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every other call in this module. Callers are expected to
    backfill the parent Policy's tree first (D-7) so `standard_status` is
    never spuriously `NULL` by the time a draft-status gate inspects it --
    this function itself does not backfill (mirrors `read_policy_tree`'s own
    division of labor: `graph_writer` issues Cypher only, backfill-then-read
    ordering is the service layer's job).

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: The Standard id to read, plus its parent Policy's
            owner/status.

    Returns:
        A `StandardWithParent` row, or `None` if no `Policy
        -[:SUPPORTED_BY]-> Standard {id: standard_id}` path exists (either
        the Standard id is unknown, or it exists but has no parent Policy --
        both are treated as "not found" by this function's own single
        `MATCH`, since every real Standard always has exactly one parent).
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
        "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title",
        params={"standard_id": standard_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    policy_id, owner_subject, owner_issuer, policy_status, standard_status, standard_title = rows[0]
    return StandardWithParent(
        policy_id=cast("str", policy_id),
        policy_owner_subject=cast("str", owner_subject),
        policy_owner_issuer=cast("str", owner_issuer),
        policy_status=cast("str", policy_status),
        standard_status=cast("str", standard_status),
        standard_title=cast("str", standard_title),
    )


def update_standard_fields(
    graph: GraphHandle, *, standard_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Standard's own content fields (issue #136, Slice 3, AC-BI-008).

    Identical null-clearing split to `update_policy_fields` (CHANGES.md
    finding #6): a single `SET s += $set_properties` for every non-`None`
    value, then one separate `SET s.<key> = null` per explicitly-`None` key
    -- no in-repo precedent exists for FalkorDB's `+=` map-merge clearing a
    property via an explicit `null` value in the map, so this does not rely
    on that.

    Property KEY NAMES are never caller-controlled strings here: `properties`
    is only ever `mcp_interface.mcp_server._parse_patch_fields`'s already-
    validated output, whose keys are drawn exclusively from a fixed,
    code-defined allow-list (`service._STANDARD_PATCHABLE_FIELDS`) -- same
    f-string-interpolation safety argument as `update_policy_fields`'s own
    docstring.

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: The existing Standard id to patch.
        properties: Field name -> new value (or `None` to clear), already
            restricted to the allowed patchable-field set by the caller.
    """
    set_properties = {key: value for key, value in properties.items() if value is not None}
    null_keys = [key for key, value in properties.items() if value is None]
    if set_properties:
        _execute_query(
            graph,
            "MATCH (s:Standard {id: $standard_id}) SET s += $set_properties",
            params={"standard_id": standard_id, "set_properties": set_properties},
        )
    for key in null_keys:
        _execute_query(
            graph,
            f"MATCH (s:Standard {{id: $standard_id}}) SET s.{key} = null",
            params={"standard_id": standard_id},
        )


def add_control_to_standard(
    graph: GraphHandle,
    *,
    standard_id: str,
    control_id: str,
    title: str,
    control_type: str,
    extra_properties: Mapping[str, object],
) -> None:
    """Mint a new Control under an existing draft Standard (issue #136, Slice 4, AC-BI-007).

    Mirrors `add_standard_to_policy` (Slice 2) exactly, one level deeper:
    ownership/status gating for this call is Slice 3's own **one-hop**
    `find_standard_with_parent` traversal (the caller supplies a
    `standard_id`, just like `update-standard-draft` does -- PLAN.md §1.4's
    own correction: this is NOT the two-hop case, that's `update-control-
    draft` alone, reached via a `control_id`). This function itself issues
    no read, only the write.

    Like `add_standard_to_policy`, a newly-minted node has no prior value to
    clear -- an explicit `None` in `extra_properties` simply means "don't set
    this field at all", never a `SET c.<key> = null` statement (CHANGES.md
    finding #6's own carve-out for add/create functions). `None`-valued keys
    are filtered out before the merge.

    `status` (governance status) is always forced to `"draft"`,
    unconditionally (D-6 parity, same as every other node-minting path in
    this module). `implementation_status` defaults to `"planned"` -- NOT
    `"draft"` like `add_standard_to_policy`'s own default -- matching
    `ps-domain-concepts.md`'s own "earliest state in status workflow"
    convention for Control (AC-BI-007's own implementation-decisions text)
    -- unless the caller already supplied one in `extra_properties` (already
    validated against `service._CONTROL_IMPLEMENTATION_STATUS_VALUES` by the
    MCP-boundary parser). `type` is always set from the caller's own
    `control_type` argument, never from `extra_properties` (CHANGES.md
    finding #8 -- the MCP-boundary parser already excludes `"type"` from
    `add-control-to-draft`'s own patchable-field allow-list, so this is
    belt-and-braces, not the only enforcement). Both `title`/`status`/`type`
    are set AFTER `extra_properties`'s own values, so none could ever be
    overridden by a caller-supplied value even if the allow-list exclusion
    were ever bypassed -- defence in depth, matching this module's own
    established convention.

    Property KEY NAMES are never caller-controlled strings here -- this
    function issues one fully-parameterized statement, no f-string
    key-interpolation is needed.

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: The existing draft Standard id to attach this Control to.
        control_id: The already-computed new Control id
            (`domain_mapper.identity.control_id(standard_id, title)`).
        title: The new Control's title.
        control_type: The new Control's `type` (`"automated"` or `"manual"`),
            already validated against `mcp_interface.mcp_server.
            _CREATE_POLICY_DRAFT_CONTROL_TYPES` at the MCP boundary.
        extra_properties: Any additional patchable content fields the
            caller supplied at creation time (already restricted to
            `service._CONTROL_PATCHABLE_FIELDS - {"type"}` by the
            MCP-boundary parser) -- `None`-valued keys are dropped, never
            written.
    """
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "planned")
    properties["title"] = title
    properties["type"] = control_type
    properties["status"] = "draft"
    _execute_query(
        graph,
        "MATCH (s:Standard {id: $standard_id}) "
        "MERGE (s)-[:IMPLEMENTED_BY]->(c:Control {id: $control_id}) "
        "SET c += $properties",
        params={"standard_id": standard_id, "control_id": control_id, "properties": properties},
    )


@dataclass(frozen=True, slots=True)
class ControlWithParent:
    """A Control's own status/title plus its root Policy's owner/status (issue #136, Slice 5).

    TASK.md's Implementation-decisions paragraph: `update-control-draft` is
    the one genuinely **two-hop** transitive-ownership tool in this issue
    (`Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control`) --
    unlike `update-standard-draft`/`add-control-to-draft` (Slices 3/4), which
    both take a `standard_id` and only need the one-hop
    `find_standard_with_parent` traversal. Control has no
    `owner_subject`/`owner_issuer` property of its own, nor does its parent
    Standard -- ownership is derived by walking all the way to the root
    Policy. `standard_id` is carried too, even though this issue's tools
    never need it directly, mirroring `StandardWithParent`'s own
    "full traversal result, no second read" shape.
    """

    standard_id: str
    policy_id: str
    policy_owner_subject: str
    policy_owner_issuer: str
    policy_status: str
    control_status: str
    control_title: str


def find_control_with_parent(graph: GraphHandle, control_id: str) -> ControlWithParent | None:
    """Two-hop transitive-ownership read for an existing Control (issue #136, Slice 5).

    A pure read, issued through the same `_execute_query` health-tracking
    wrapper as every other call in this module. Callers are expected to
    backfill the root Policy's tree first (D-7) so `control_status` is
    never spuriously `NULL` by the time a draft-status gate inspects it --
    this function itself does not backfill (mirrors
    `find_standard_with_parent`'s own division of labor: `graph_writer`
    issues Cypher only, backfill-then-read ordering is the service layer's
    job).

    Args:
        graph: The single-tenant policy graph handle.
        control_id: The Control id to read, plus its parent Standard id and
            root Policy's owner/status.

    Returns:
        A `ControlWithParent` row, or `None` if no `Policy
        -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control {id:
        control_id}` path exists (either the Control id is unknown, or it
        exists but its parent chain is broken -- both are treated as "not
        found" by this function's own single `MATCH`, since every real
        Control always has exactly one parent Standard and one root Policy).
    """
    result = _execute_query(
        graph,
        "MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->"
        "(c:Control {id: $control_id}) "
        "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title",
        params={"control_id": control_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    (
        standard_id,
        policy_id,
        owner_subject,
        owner_issuer,
        policy_status,
        control_status,
        control_title,
    ) = rows[0]
    return ControlWithParent(
        standard_id=cast("str", standard_id),
        policy_id=cast("str", policy_id),
        policy_owner_subject=cast("str", owner_subject),
        policy_owner_issuer=cast("str", owner_issuer),
        policy_status=cast("str", policy_status),
        control_status=cast("str", control_status),
        control_title=cast("str", control_title),
    )


def update_control_fields(
    graph: GraphHandle, *, control_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Control's own content fields (issue #136, Slice 5, AC-BI-008).

    Identical null-clearing split to `update_policy_fields`/
    `update_standard_fields` (CHANGES.md finding #6): a single
    `SET c += $set_properties` for every non-`None` value, then one separate
    `SET c.<key> = null` per explicitly-`None` key -- no in-repo precedent
    exists for FalkorDB's `+=` map-merge clearing a property via an explicit
    `null` value in the map, so this does not rely on that.

    Property KEY NAMES are never caller-controlled strings here: `properties`
    is only ever `mcp_interface.mcp_server._parse_patch_fields`'s already-
    validated output, whose keys are drawn exclusively from a fixed,
    code-defined allow-list (`service._CONTROL_PATCHABLE_FIELDS`) -- same
    f-string-interpolation safety argument as `update_policy_fields`'s own
    docstring.

    Args:
        graph: The single-tenant policy graph handle.
        control_id: The existing Control id to patch.
        properties: Field name -> new value (or `None` to clear), already
            restricted to the allowed patchable-field set by the caller.
    """
    set_properties = {key: value for key, value in properties.items() if value is not None}
    null_keys = [key for key, value in properties.items() if value is None]
    if set_properties:
        _execute_query(
            graph,
            "MATCH (c:Control {id: $control_id}) SET c += $set_properties",
            params={"control_id": control_id, "set_properties": set_properties},
        )
    for key in null_keys:
        _execute_query(
            graph,
            f"MATCH (c:Control {{id: $control_id}}) SET c.{key} = null",
            params={"control_id": control_id},
        )
