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

`backfill_governance_status` (GH issue #134, D-7/S8; a gateway read-resolve-write since #211):
the idempotent, self-healing backfill for Standard/Control's `status` property and Policy's
`version` property. No schema/data-migration mechanism exists in this codebase
(`docs/architecture/ps-solution-architecture.md`'s own risk-register row), so it runs at the top
of every lifecycle read/gate call: it READS which of those properties are NULL on the Policy's tree
and submits ONE gateway group of `MergeProperty` for exactly the nodes that need a fill (no audit
link: a backfill has no audit row). A tree with nothing to fill submits nothing, not even the
legacy-edge stamp, so a re-run is a no-op.

Own copy of the `GraphHandle`/`GraphQueryResult` Protocols + `_read_query`
dependency-health wrapper shape already vendored independently by
`ps_service.company_merge.graph_writer`, `ps_service.domain_mapper.graph_writer`,
and `ps_service.ingestion.adapters.internal_seed.persist` (L2's "fully
decoupled, no shared internal package between components" rule) -- a
deliberate near-duplicate, not a shared import.

**Reads only.** Since issue #211 no statement in this module writes: every write is a Graph Write
Gateway group built from primitives and submitted through `policy_lifecycle.graph_submission`. A
FalkorDB failure on a READ is still recorded in `dependency_health` (for `/ready`'s live signal) and
re-raised as the original `redis.exceptions.RedisError`, unwrapped; a failed WRITE is the module's
`PolicyLifecycleGraphUnavailableError` with the `FalkorDB write failed: ` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.models import (
    DeleteEdge,
    ExpectedPosition,
    MergeProperty,
    NodeRef,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)
from ps_service.policy_lifecycle.errors import StaleGroupError
from ps_service.policy_lifecycle.graph_submission import (
    GraphWriteTarget,
    edge_identity,
    ensure_stamped,
    read_log_position,
    refuse_unstorable_values,
    require_caught_up,
    single_tenant_graph_name,
    submit_group,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.graph_gateway.models import Primitive
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter

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
    "create_claimed_policy_draft",
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

    Only the rows are used: each read in this module takes them from `result_set`.
    """

    @property
    def result_set(self) -> list[object]:
        """Result rows exactly as returned by `falkordb`."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`, used for reads only (not enforced by the type).

    Every lifecycle write is a Graph Write Gateway group (issue #211); the static guard in
    `tests/policy_lifecycle/test_no_graph_write_path.py` is what keeps write Cypher out.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


def _read_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every graph statement in this module goes through.

    Mirrors every other `graph_writer`-shaped module's own `_read_query`:
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


_READ_BACKFILL_STATE = (
    "MATCH (p:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
    "RETURN p.version, p.status, s.id, s.status, c.id, c.status"
)
"""What a backfill reads: the Policy's version and status, and each Standard's and Control's."""


def backfill_governance_status(
    graph: GraphHandle, policy_id: str, *, submitter_source: Callable[[], LifecycleSubmitter]
) -> None:
    """Idempotently backfill `policy_id`'s own `version` and its tree's `status` (D-7).

    A read-resolve-write (issue #211). One read returns the Policy's `version` and `status` and the
    `status` of every Standard and Control in its `SUPPORTED_BY`/`IMPLEMENTED_BY` tree; the fills
    are then resolved in Python and submitted as ONE gateway group with no audit link:

    1. `policy_id`'s own `version` -> `"1"`, only if currently `NULL`.
    2. Every `Standard` whose `status` is `NULL` -> the Policy's own current `status`.
    3. Every `Control` whose `status` is `NULL` -> the Policy's own current `status` -- always the
       ROOT Policy's `status`, never an intermediate Standard's.

    A Standard/Control that already carries a non-`NULL` `status` is never touched, even if it
    differs from the Policy's current `status` -- the guard is a one-time-fill mechanism, not a
    continuous sync. A Policy whose own `status` is `NULL` has nothing to hand its children.

    Nothing to fill (the common case) submits no group and stamps no legacy edge (AC-BI-006), and
    never asks for the submitter: `submitter_source` is called only when there is a group to write,
    so a read of an already-backfilled tree needs no gateway (D3).

    Args:
        graph: The single-tenant policy graph handle (read).
        policy_id: The Policy whose tree is backfilled; an unknown id is a no-op.
        submitter_source: Resolves the Graph Write Gateway; called only if a fill is needed.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the fill group failed.
    """
    result = _read_query(graph, _READ_BACKFILL_STATE, params={"policy_id": policy_id})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return
    version, policy_status = rows[0][0], rows[0][1]
    primitives: list[Primitive] = []
    if version is None:
        primitives.append(MergeProperty(label="Policy", id=policy_id, properties={"version": "1"}))
    if policy_status is not None:
        seen: set[tuple[str, str]] = set()
        for _, _, standard_id, standard_status, control_id, control_status in rows:
            for label, node_id, status in (
                ("Standard", standard_id, standard_status),
                ("Control", control_id, control_status),
            ):
                if node_id is None or status is not None or (label, cast("str", node_id)) in seen:
                    continue
                seen.add((label, cast("str", node_id)))
                primitives.append(
                    MergeProperty(
                        label=label,
                        id=cast("str", node_id),
                        properties={"status": policy_status},
                    )
                )
    if not primitives:
        return
    target = GraphWriteTarget(submitter_source(), single_tenant_graph_name())
    submit_group(target, tuple(primitives), audit_event_id=None)


def _cascade_primitives(
    graph: GraphHandle, *, policy_id: str, target_status: str
) -> tuple[Primitive, ...]:
    """A `MergeProperty status` for the Policy and every Standard/Control of its tree, as read now.

    The tree is read at the moment of the call (the caller submits right after), so a Standard
    added since the transition's gate read is cascaded too -- no lock, as before (AC-BI-003). A
    Policy that is gone yields nothing, as the former statement matched nothing.
    """
    record = read_policy_tree(graph, policy_id)
    if record is None:
        return ()
    nodes: list[tuple[str, str]] = [("Policy", record.id)]
    for standard in record.standards:
        nodes.append(("Standard", standard.id))
        nodes.extend(("Control", control.id) for control in standard.controls)
    return tuple(
        MergeProperty(label=label, id=node_id, properties={"status": target_status})
        for label, node_id in dict.fromkeys(nodes)
    )


def cascade_status(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    policy_id: str,
    target_status: str,
    audit_event_id: str,
) -> None:
    """Set `policy_id`'s own `status`, plus every Standard/Control in its tree, to `target_status`.

    Issue #134, S15 -- the shared "cascade to a target status" write every lifecycle transition
    (`propose`/`approve`/`reject`/`revert`/`auto_deprecate`) needs. Since issue #211 it is ONE
    Graph Write Gateway group of `MergeProperty status` over the tree as read just before the
    submit (AC-BI-003): atomic like the former single statement, with no precondition and no lock.
    The gateway skips a node already at `target_status`; a tree entirely at it logs nothing. The
    group is linked to the `applied` audit row `audit_event_id` names.

    Unlike `backfill_governance_status`'s fill of NULLs, this overwrites every `status`
    unconditionally -- a lifecycle transition intentionally replaces whatever was there.

    Args:
        graph: The single-tenant policy graph handle (the tree read).
        target: The gateway target of the single-tenant policy graph.
        policy_id: The root Policy id whose tree is being transitioned.
        target_status: The new `status` value for the Policy and its entire Standard/Control tree
            (e.g. `"proposed"`, `"approved"`, `"deprecated"`).
        audit_event_id: The id of the transition's `applied` audit row.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    primitives = _cascade_primitives(graph, policy_id=policy_id, target_status=target_status)
    submit_group(target, primitives, audit_event_id=audit_event_id)


def find_approved_prior(graph: GraphHandle, successor_policy_id: str) -> str | None:
    """Find an `approved` Policy that `SUPERSEDED_BY`s `successor_policy_id` (D-10, S24).

    A pure read, issued through the same `_read_query` health-tracking
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
    result = _read_query(
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
    result = _read_query(
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
    target: GraphWriteTarget,
    *,
    policy_id: str,
    prior_id: str,
    capability_ids: tuple[str, ...],
    target_status: str,
    audit_event_id: str,
) -> bool:
    """Move `GOVERNED_BY` edges to `policy_id` and cascade its status, as one guarded group.

    Issue #185 introduced the move; since issue #211 it is a gateway read-resolve-write in the
    order of `ingestion/adapters/internal_seed/persist.py` (CHANGES M1): the graph must be caught
    up, the log position P0 is read, then the prior's governed set is re-read and compared to
    `capability_ids` in Python -- a set that changed since the caller's read returns `False` with
    NOTHING logged, not even a legacy-edge stamp -- and only then are legacy edges stamped (a
    group with no audit link; `GOVERNED_BY` edges written before the gateway have no identity to
    delete by) and the group submitted with `ExpectedPosition(P0 + stamped)`. A write that landed
    between the position read and the submit trips that precondition: the same `False`. The
    position guard is coarser than the former in-statement guard (Decision 2).

    The group deletes each `GOVERNED_BY` edge from the prior, upserts it from the same Capability
    to the fork, and cascades `target_status` over the fork's tree, so a Capability never has zero
    or two governors; it is linked to the `applied` audit row `audit_event_id` names.

    Args:
        graph: The single-tenant policy graph handle (the guard's reads).
        target: The gateway target of the single-tenant policy graph.
        policy_id: The fork being approved (the new governor).
        prior_id: The superseded Policy that currently governs the Capabilities.
        capability_ids: The Capabilities read from `prior_id` beforehand.
        target_status: The status to cascade over the fork's tree.
        audit_event_id: The id of the `applied` approval audit row.

    Returns:
        `True` when the move and cascade were applied, `False` when the guard failed (nothing of
        this group was written).

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    governance = read_fork_governance(graph, policy_id)
    if (
        governance is None
        or governance.prior_id != prior_id
        or sorted(governance.capability_ids) != sorted(capability_ids)
    ):
        return False
    stamped = ensure_stamped(target, None)  # housekeeping: no audit link, no data change (A1)
    fork_ref = NodeRef(label="Policy", id=policy_id)
    prior_ref = NodeRef(label="Policy", id=prior_id)
    moved = dict.fromkeys(capability_ids)
    primitives: list[Primitive] = [
        DeleteEdge(
            type="GOVERNED_BY",
            identity=edge_identity("GOVERNED_BY", capability_id, prior_id),
            source=NodeRef(label="Capability", id=capability_id),
            target=prior_ref,
        )
        for capability_id in moved
    ]
    primitives.extend(
        UpsertEdge(
            type="GOVERNED_BY",
            identity=edge_identity("GOVERNED_BY", capability_id, policy_id),
            source=NodeRef(label="Capability", id=capability_id),
            target=fork_ref,
        )
        for capability_id in moved
    )
    primitives.extend(_cascade_primitives(graph, policy_id=policy_id, target_status=target_status))
    try:
        submit_group(
            target,
            tuple(primitives),
            audit_event_id=audit_event_id,
            preconditions=(ExpectedPosition(position=expected_position + stamped),),
        )
    except StaleGroupError:
        return False
    return True


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

    A pure read, issued through the same `_read_query` health-tracking
    wrapper as every write in this module -- a title-collision check must
    still count towards `/ready`'s FalkorDB connectivity signal.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The v1 `policy_id(title)` the caller is about to mint.

    Returns:
        `(existing_id, existing_title)` if a `Policy` node with this id
        already exists, else `None`.
    """
    result = _read_query(
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
    not exist, or is a `merged` tombstone (issue #190), is absent from the
    result; an existing but ungoverned one maps
    to `None`.

    Args:
        graph: The single-tenant policy graph handle.
        capability_ids: The Capability ids a fresh draft wants to claim.

    Returns:
        `{capability_id: governing_policy_id | None}` for every id that exists.
    """
    result = _read_query(
        graph,
        "MATCH (cap:Capability) WHERE cap.id IN $capability_ids "
        "AND coalesce(cap.status,'active') <> 'merged' "
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
    target: GraphWriteTarget,
    *,
    policy_id: str,
    title: str,
    owner: tuple[str, str],
    standards: tuple[StandardDraft, ...] = (),
    supersedes_policy_id: str | None = None,
    version: str = "1",
    audit_event_id: str,
) -> None:
    """Mint a new draft Policy, plus any optional Standard/Control children, as ONE gateway group.

    Without a Capability claim (a claim is `create_claimed_policy_draft` until it migrates). The
    group, in order, is: an `UpsertNode` for the Policy (`status="draft"`, `version`, the owner
    pair), the optional `SUPERSEDED_BY:{prior}->{new}` edge, then for each Standard an
    `UpsertNode` and its `SUPPORTED_BY:{policy}->{standard}` edge and for each of its Controls an
    `UpsertNode` and its `IMPLEMENTED_BY:{standard}->{control}` edge. It is linked to the `applied`
    audit row that `audit_event_id` names (AC-BI-008). The group is atomic: a failure leaves no
    partial draft.

    Every child is written with `status="draft"` unconditionally (D-6). A child's
    `extra_properties` (when the caller is forking, the source tree's full content minus
    `id`/`title`/`status`) are spread BEFORE `title`/`type`/`status`, so a stray copied
    `title`/`status`/`type` can never win. A value the gateway cannot store (a reserved key such as
    `embedding`, an empty list) is refused before anything is written.

    `supersedes_policy_id` links the prior Policy to this new one by a single Policy-level edge --
    AC-BI-005's "no per-Standard/Control lineage edges" requirement.

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner: The creating actor's `(sub, iss)`.
        standards: Optional Standard children (each with its own optional Control children), ids
            already computed by the caller.
        supersedes_policy_id: The prior Policy id this new draft amends via `SUPERSEDED_BY`, or
            `None` for an ordinary (non-fork) draft.
        version: The new Policy's own `version` (string-typed, per `docs/artifacts`'s "version
            stays a string" decision): `"1"` for an ordinary draft, or `str(int(prior_version) + 1)`
            for a fork (computed by the caller).
        audit_event_id: The id of the `applied` `policy.create_draft` row the group is linked to.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    primitives = _create_primitives(
        policy_id=policy_id,
        title=title,
        owner=owner,
        standards=standards,
        supersedes_policy_id=supersedes_policy_id,
        version=version,
    )
    submit_group(target, primitives, audit_event_id=audit_event_id)


def _create_primitives(
    *,
    policy_id: str,
    title: str,
    owner: tuple[str, str],
    standards: tuple[StandardDraft, ...],
    supersedes_policy_id: str | None,
    version: str,
) -> tuple[Primitive, ...]:
    """The primitives of a create: the Policy, its optional supersede edge, then the children."""
    owner_subject, owner_issuer = owner
    policy_ref = NodeRef(label="Policy", id=policy_id)
    primitives: list[Primitive] = []
    with refuse_unstorable_values():
        primitives.append(
            UpsertNode(
                label="Policy",
                id=policy_id,
                properties={
                    "title": title,
                    "status": "draft",
                    "version": version,
                    "owner_subject": owner_subject,
                    "owner_issuer": owner_issuer,
                },
            )
        )
        if supersedes_policy_id is not None:
            primitives.append(
                UpsertEdge(
                    type="SUPERSEDED_BY",
                    identity=edge_identity("SUPERSEDED_BY", supersedes_policy_id, policy_id),
                    source=NodeRef(label="Policy", id=supersedes_policy_id),
                    target=policy_ref,
                )
            )
        for standard in standards:
            standard_properties: dict[str, object] = dict(standard.extra_properties)
            standard_properties["title"] = standard.title
            standard_properties["status"] = "draft"
            primitives.extend(
                _child_primitives(
                    parent=policy_ref,
                    label="Standard",
                    child_id=standard.id,
                    properties=standard_properties,
                    edge_type="SUPPORTED_BY",
                )
            )
            standard_ref = NodeRef(label="Standard", id=standard.id)
            for control in standard.controls:
                control_properties: dict[str, object] = dict(control.extra_properties)
                control_properties["title"] = control.title
                control_properties["type"] = control.control_type
                control_properties["status"] = "draft"
                primitives.extend(
                    _child_primitives(
                        parent=standard_ref,
                        label="Control",
                        child_id=control.id,
                        properties=control_properties,
                        edge_type="IMPLEMENTED_BY",
                    )
                )
    return tuple(primitives)


def create_claimed_policy_draft(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    policy_id: str,
    title: str,
    owner: tuple[str, str],
    standards: tuple[StandardDraft, ...] = (),
    capability_ids: tuple[str, ...],
    audit_event_id: str,
) -> bool:
    """Mint a draft Policy that CLAIMS `capability_ids` as one guarded gateway group.

    The guard is the one of `ingestion/adapters/internal_seed/persist.py`, in its order (CHANGES
    M1, `graph_submission` docstring): the graph must be caught up, the log position P0 is read,
    then `read_capability_governors` re-resolves every claimed Capability in Python (it exists, is
    not a `merged` tombstone, and has no governing Policy) -- a claim that no longer holds returns
    `False` here with NOTHING logged, not even a legacy-edge stamp -- and only then are the
    graph's legacy edges stamped (a group with no audit link) and the group submitted with
    `ExpectedPosition(P0 + stamped)`. A write that landed between the position read and the
    submit trips that precondition: the same `False`, nothing of this group logged (a stamp group
    ahead of it, which carries no audit link and changes no data, may stay in the log). The
    position guard is coarser than the former in-statement guard: an UNRELATED write in that
    window also refuses the claim (Decision 2).

    The group holds an `UpsertNode` for the Policy, an `UpsertEdge`
    `GOVERNED_BY:{capability}->{policy}` per Capability and the Standard/Control children as
    `create_policy_draft` writes them; it is linked to the `applied` audit row `audit_event_id`
    names. A claim is a fresh `version` "1" draft, never combined with a supersede fork.

    Args:
        graph: The single-tenant policy graph handle (the guard's reads).
        target: The gateway target of the single-tenant policy graph.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner: The creating actor's `(sub, iss)`.
        standards: Optional Standard children (each with its own optional Control children).
        capability_ids: Capabilities the draft claims, de-duplicated, order-preserving.
        audit_event_id: The id of the `applied` `policy.create_draft` row the group is linked to.

    Returns:
        `True` when the draft was written; `False` when the claim no longer held (a Capability was
        governed, merged or removed after the caller's pre-check, or the position moved) -- in which
        case no Policy, edge, Standard or Control was written.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    governors = read_capability_governors(graph, capability_ids)
    if any(governors.get(capability_id, "") is not None for capability_id in capability_ids):
        return False
    stamped = ensure_stamped(target, None)  # housekeeping: no audit link, no data change (A1)
    policy_ref = NodeRef(label="Policy", id=policy_id)
    primitives: list[Primitive] = list(
        _create_primitives(
            policy_id=policy_id,
            title=title,
            owner=owner,
            standards=standards,
            supersedes_policy_id=None,
            version="1",
        )
    )
    with refuse_unstorable_values():
        primitives.extend(
            UpsertEdge(
                type="GOVERNED_BY",
                identity=edge_identity("GOVERNED_BY", capability_id, policy_id),
                source=NodeRef(label="Capability", id=capability_id),
                target=policy_ref,
            )
            for capability_id in capability_ids
        )
    try:
        submit_group(
            target,
            tuple(primitives),
            audit_event_id=audit_event_id,
            preconditions=(ExpectedPosition(position=expected_position + stamped),),
        )
    except StaleGroupError:
        return False
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

    A pure read, issued through the same `_read_query` health-tracking
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
    result = _read_query(
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

    A pure read, issued through the same `_read_query` health-tracking
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
    result = _read_query(
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
    target: GraphWriteTarget, *, policy_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Policy's own content fields (issue #136, Slice 1, AC-BI-008).

    An explicit `None` value in `properties` means "clear this field" (a legitimate PATCH
    semantic, distinct from the key being absent entirely, which leaves the existing value
    untouched -- the caller already only includes keys it means to change). Since issue #211 the
    patch is ONE gateway group with no audit link (a draft edit writes no `audit_events` row): a
    `MergeProperty` for every non-`None` value, then a `RemoveProperty` for every cleared key.
    A patch that sets nothing has no merge, one that clears nothing has no remove, and a patch
    that changes nothing logs nothing.

    Property KEY NAMES are never caller-controlled strings here: `properties` is only ever
    `mcp_interface.mcp_server._parse_patch_fields`'s already-validated output, whose keys are
    drawn exclusively from a fixed, code-defined allow-list (`service._POLICY_PATCHABLE_FIELDS`,
    derived from `ps_service.domain_schema`); the gateway validates each key again against its
    identifier pattern before it reaches Cypher.

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The existing Policy id to patch.
        properties: Field name -> new value (or `None` to clear), already restricted to the
            allowed patchable-field set by the caller.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused
            the group (for example a value it cannot store), could not reach its stores, or the
            group was committed but not applied.
    """
    submit_group(
        target,
        _patch_primitives("Policy", policy_id, properties),
        audit_event_id=None,
    )


def _patch_primitives(
    label: str, node_id: str, properties: Mapping[str, object]
) -> tuple[Primitive, ...]:
    """The primitives of one PATCH: a merge of the set values, then a remove of the cleared keys."""
    set_properties = {key: value for key, value in properties.items() if value is not None}
    null_keys = tuple(key for key, value in properties.items() if value is None)
    primitives: list[Primitive] = []
    with refuse_unstorable_values():
        if set_properties:
            primitives.append(MergeProperty(label=label, id=node_id, properties=set_properties))
        if null_keys:
            primitives.append(RemoveProperty(label=label, id=node_id, keys=null_keys))
    return tuple(primitives)


def _child_primitives(
    *,
    parent: NodeRef,
    label: str,
    child_id: str,
    properties: Mapping[str, object],
    edge_type: str,
) -> tuple[Primitive, ...]:
    """The primitives that mint a child under `parent`: the node, then the parent's edge to it."""
    with refuse_unstorable_values():
        return (
            UpsertNode(label=label, id=child_id, properties=dict(properties)),
            UpsertEdge(
                type=edge_type,
                identity=edge_identity(edge_type, parent.id, child_id),
                source=parent,
                target=NodeRef(label=label, id=child_id),
            ),
        )


def add_standard_to_policy(
    target: GraphWriteTarget,
    *,
    policy_id: str,
    standard_id: str,
    title: str,
    extra_properties: Mapping[str, object],
) -> None:
    """Mint a new Standard under an existing draft Policy (issue #136, Slice 2, AC-BI-007).

    Unlike `update_policy_fields`, a newly-minted node has no prior value to clear: an explicit
    `None` in `extra_properties` (a caller passing `fields={"description": None}` to
    `add-standard-to-draft`, say) simply means "don't set this field at all". `None`-valued keys
    are filtered out.

    `status` (governance status) is always forced to `"draft"`, unconditionally, whatever
    `extra_properties` holds. `implementation_status` defaults to `"draft"` (AC-BI-007) unless the
    caller supplied it (already validated against `service._STANDARD_IMPLEMENTATION_STATUS_VALUES`
    by the MCP-boundary parser). Both `title` and `status` are set AFTER `extra_properties`'s
    values, so neither could be overridden by a caller-supplied value (defence in depth).

    Since issue #211 this is ONE gateway group with no audit link: an `UpsertNode` for the
    Standard, then the `UpsertEdge` `SUPPORTED_BY:{policy}->{standard}` from its parent. The
    Standard's id derives from its parent and title (`domain_mapper.identity.standard_id`), so
    adding the same Standard twice under one parent changes nothing the second time (AC-BI-009).

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The existing draft Policy id to attach this Standard to.
        standard_id: The already-computed new Standard id
            (`domain_mapper.identity.standard_id(policy_id, title)`).
        title: The new Standard's title.
        extra_properties: Any additional patchable content fields the caller supplied at
            creation time (already restricted to `service._STANDARD_PATCHABLE_FIELDS` by the
            MCP-boundary parser); `None`-valued keys are dropped, never written.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "draft")
    properties["title"] = title
    properties["status"] = "draft"
    submit_group(
        target,
        _child_primitives(
            parent=NodeRef(label="Policy", id=policy_id),
            label="Standard",
            child_id=standard_id,
            properties=properties,
            edge_type="SUPPORTED_BY",
        ),
        audit_event_id=None,
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

    A pure read, issued through the same `_read_query` health-tracking
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
    result = _read_query(
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
    target: GraphWriteTarget, *, standard_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Standard's own content fields (issue #136, Slice 3, AC-BI-008).

    Same split as `update_policy_fields`: one gateway group with no audit link, a `MergeProperty`
    for every non-`None` value then a `RemoveProperty` for every cleared key; a patch that
    changes nothing logs nothing. `properties` is only ever
    `mcp_interface.mcp_server._parse_patch_fields`'s already-validated output, whose keys come
    from the code-defined allow-list (`service._STANDARD_PATCHABLE_FIELDS`, derived from
    `ps_service.domain_schema`).

    Args:
        target: The gateway target of the single-tenant policy graph.
        standard_id: The existing Standard id to patch.
        properties: Field name -> new value (or `None` to clear), already restricted to the
            allowed patchable-field set by the caller.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    submit_group(
        target, _patch_primitives("Standard", standard_id, properties), audit_event_id=None
    )


def add_control_to_standard(
    target: GraphWriteTarget,
    *,
    standard_id: str,
    control_id: str,
    title: str,
    control_type: str,
    extra_properties: Mapping[str, object],
) -> None:
    """Mint a new Control under an existing draft Standard (issue #136, Slice 4, AC-BI-007).

    Mirrors `add_standard_to_policy` one level deeper (ownership and status gating for the call
    is the service's one-hop `find_standard_with_parent`; this function issues no read).

    `None`-valued keys of `extra_properties` are dropped. `status` is always forced to `"draft"`.
    `implementation_status` defaults to `"planned"` (NOT `"draft"` like a Standard's) unless the
    caller supplied one. `type` is always set from `control_type`, never from `extra_properties`.
    `title`, `type` and `status` are set AFTER `extra_properties`'s values, so none could be
    overridden by a caller-supplied value (defence in depth).

    Since issue #211 this is ONE gateway group with no audit link: an `UpsertNode` for the Control,
    then the `UpsertEdge` `IMPLEMENTED_BY:{standard}->{control}` from its parent.

    Args:
        target: The gateway target of the single-tenant policy graph.
        standard_id: The existing draft Standard id to attach this Control to.
        control_id: The already-computed new Control id
            (`domain_mapper.identity.control_id(standard_id, title)`).
        title: The new Control's title.
        control_type: The new Control's `type` (`"automated"` or `"manual"`), already validated
            at the MCP boundary.
        extra_properties: Any additional patchable content fields the caller supplied at
            creation time (already restricted to `service._CONTROL_PATCHABLE_FIELDS - {"type"}`
            by the MCP-boundary parser); `None`-valued keys are dropped, never written.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "planned")
    properties["title"] = title
    properties["type"] = control_type
    properties["status"] = "draft"
    submit_group(
        target,
        _child_primitives(
            parent=NodeRef(label="Standard", id=standard_id),
            label="Control",
            child_id=control_id,
            properties=properties,
            edge_type="IMPLEMENTED_BY",
        ),
        audit_event_id=None,
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

    A pure read, issued through the same `_read_query` health-tracking
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
    result = _read_query(
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
    target: GraphWriteTarget, *, control_id: str, properties: Mapping[str, object]
) -> None:
    """PATCH a subset of an existing Control's own content fields (issue #136, Slice 5, AC-BI-008).

    Identical to `update_standard_fields` for a Control: one gateway group with no audit link,
    keys from `service._CONTROL_PATCHABLE_FIELDS`.

    Args:
        target: The gateway target of the single-tenant policy graph.
        control_id: The existing Control id to patch.
        properties: Field name -> new value (or `None` to clear), already restricted to the
            allowed patchable-field set by the caller.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
    """
    submit_group(target, _patch_primitives("Control", control_id, properties), audit_event_id=None)
