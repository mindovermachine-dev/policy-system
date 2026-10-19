"""Reads and gateway writes behind the Policy lifecycle in the single-tenant policy graph.

Reads (`find_*`, `read_*`) issue Cypher through a `GraphHandle` and return typed records. No
statement in this module writes: every write is a Graph Write Gateway group built from primitives
and submitted through `ps_service.policy_lifecycle.graph_submission`. `ps_service.policy_lifecycle
.service` calls these functions and owns the lifecycle decisions, audit rows and error translation.

The main operations:

- `backfill_governance_status` fills a Policy's missing `version` and its tree's missing `status`;
  it runs at the top of every lifecycle read or gate call because no schema or data-migration
  mechanism exists in this codebase (`docs/architecture/ps-solution-architecture.md`, risk
  register). It reads which properties are NULL and submits one group for exactly the nodes that
  need a fill; a tree with nothing to fill submits nothing, not even the legacy-edge stamp, so a
  re-run is a no-op.
- `create_policy_draft`, `create_claimed_policy_draft`, `add_standard_to_policy`,
  `add_control_to_standard` mint draft nodes; `update_policy_fields`, `update_standard_fields`,
  `update_control_fields` patch them; `cascade_status` and `approve_fork_repoint` apply a
  transition.
- `find_existing_policy`, `find_approved_prior`, `read_policy_tree`, `read_policy_tree_for_fork`,
  `read_fork_governance`, `read_capability_governors`, `find_standard_with_parent` and
  `find_control_with_parent` are the reads.

This module keeps its own copy of the `GraphHandle`/`GraphQueryResult` Protocols and the
health-recording `_read_query` wrapper that `ps_service.company_merge.graph_writer`,
`ps_service.domain_mapper.graph_writer` and `ps_service.ingestion.adapters.internal_seed.persist`
also vendor independently: components share no internal package, so the near-duplicate is
deliberate.

A FalkorDB failure on a read is recorded in `dependency_health` (the live signal behind `/ready`)
and re-raised as the original `redis.exceptions.RedisError`, unwrapped. A failed write surfaces as
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
    """Structural stand-in for `falkordb.QueryResult`: only the rows are used.

    Attributes:
        result_set: The result rows exactly as `falkordb` returns them.
    """

    @property
    def result_set(self) -> list[object]:
        """Return the result rows exactly as returned by `falkordb`.

        Returns:
            One list of column values per result row; empty when the statement matched nothing.

        Raises:
            Nothing: reading the rows of a completed result does not fail.

        Side Effects:
            None.
        """
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`, used by this module for reads only.

    Every lifecycle write is a Graph Write Gateway group; the static guard in
    `tests/policy_lifecycle/test_no_graph_write_path.py` keeps write Cypher out of this component.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` and return the result.

        Args:
            q: The Cypher statement; this module passes read statements only.
            params: Query parameters bound to `$name` placeholders in `q`; `None` for none.

        Returns:
            The `GraphQueryResult` holding the statement's rows.

        Raises:
            redis.exceptions.RedisError: the FalkorDB server is unreachable or the driver fails.

        Side Effects:
            Executes `q` against the graph; for the read statements this module issues, nothing is
            modified.
        """
        ...


def _read_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """Run one read statement and record FalkorDB health for `/ready`.

    A `RedisError` marks the FalkorDB dependency unhealthy and is re-raised unchanged; the next
    successful call marks it healthy again.
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
    """Fill a Policy's missing `version` and its tree's missing `status`, idempotently.

    One read returns the Policy's `version` and `status` and the `status` of every Standard and
    Control in its `SUPPORTED_BY`/`IMPLEMENTED_BY` tree. The fills are resolved in Python and
    submitted as ONE gateway group with no audit link (a backfill has no audit row):

    1. `policy_id`'s own `version` becomes `"1"`, only if currently `NULL`.
    2. Every `Standard` whose `status` is `NULL` takes the Policy's current `status`.
    3. Every `Control` whose `status` is `NULL` takes the Policy's current `status`, always the
       root Policy's, never an intermediate Standard's.

    A Standard or Control that already carries a non-`NULL` `status` is never touched, even if it
    differs from the Policy's current `status`: this is a one-time fill, not a continuous sync. A
    Policy whose own `status` is `NULL` has nothing to hand its children.

    Args:
        graph: The single-tenant policy graph handle, used for the read.
        policy_id: The Policy whose tree is backfilled; an id that matches no Policy is a no-op.
        submitter_source: Zero-argument callable that resolves the gateway submitter. It is called
            only when there is something to write, so reading an already-backfilled tree needs no
            gateway.

    Returns:
        None.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the fill group failed,
            or a value could not be stored. Any error `submitter_source` raises propagates
            unchanged.

    Side Effects:
        Reads the graph and updates `dependency_health`. When a fill is needed, submits one gateway
        group of `MergeProperty` primitives (stamping the graph's legacy edges first if this target
        has not yet been stamped); with nothing to fill, writes and logs nothing.
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
    """Build a `MergeProperty status` for the Policy and every Standard/Control of its tree.

    The tree is read at the moment of the call and the caller submits right after, so a Standard
    added since the transition's gate read is cascaded too; no lock is taken. A Policy that no
    longer exists yields no primitives.
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
    """Set the `status` of `policy_id` and of its whole Standard/Control tree to `target_status`.

    The shared write every lifecycle transition (`propose`, `approve`, `reject`, `revert`,
    `auto_deprecate`) needs. It is ONE gateway group of `MergeProperty status` over the tree as read
    just before the submit: atomic, with no precondition and no lock. The gateway skips a node
    already at `target_status`, and a tree entirely at it logs nothing. Unlike the backfill's fill
    of NULLs, this overwrites every `status` unconditionally.

    Args:
        graph: The single-tenant policy graph handle, used to read the tree.
        target: The gateway target of the single-tenant policy graph.
        policy_id: The root Policy id whose tree is transitioned.
        target_status: The new `status` for the Policy and its whole Standard/Control tree, for
            example `"proposed"`, `"approved"` or `"deprecated"`.
        audit_event_id: The id of the transition's `applied` audit row; the group is linked to it.

    Returns:
        None.

    Raises:
        redis.exceptions.RedisError: the tree read failed.
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group, could not reach its stores, or the group was committed but not applied.

    Side Effects:
        Reads the graph and updates `dependency_health`. Submits one gateway group linked to
        `audit_event_id` (stamping the graph's legacy edges first if this target has not yet been
        stamped). Nothing is submitted when the Policy no longer exists.
    """
    primitives = _cascade_primitives(graph, policy_id=policy_id, target_status=target_status)
    submit_group(target, primitives, audit_event_id=audit_event_id)


def find_approved_prior(graph: GraphHandle, successor_policy_id: str) -> str | None:
    """Find an `approved` Policy that is `SUPERSEDED_BY` `successor_policy_id`.

    A pure read. It only traverses an existing `SUPERSEDED_BY` edge; creating that edge is the fork
    tool's job. The approval flow calls it after the successor's own tree cascade, to decide whether
    the prior must also be auto-deprecated.

    Args:
        graph: The single-tenant policy graph handle.
        successor_policy_id: The Policy id that was just, or is about to be, approved; the target of
            the `SUPERSEDED_BY` edge.

    Returns:
        The `id` of an inbound `SUPERSEDED_BY` Policy whose own `status` is currently `"approved"`,
        or `None` when no such edge or prior exists. A prior in `draft` or `proposed` is
        deliberately not returned: only an already-approved prior is ever auto-deprecated.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """What a fork's superseded prior currently governs.

    Attributes:
        prior_id: The id of the Policy the fork supersedes.
        capability_ids: The ids of the Capabilities the prior governs through `GOVERNED_BY`. May be
            empty: a legacy prior with no `GOVERNED_BY` edges leaves nothing to move.
    """

    prior_id: str
    capability_ids: tuple[str, ...]


def read_fork_governance(graph: GraphHandle, policy_id: str) -> ForkGovernance | None:
    """Read the prior superseded by `policy_id` and the Capabilities it governs.

    A pure read. The prior's own status is deliberately not filtered: a prior that is not approved
    can still be superseded.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The successor (fork) Policy id about to be approved.

    Returns:
        `None` when no inbound `SUPERSEDED_BY` edge exists, otherwise a `ForkGovernance` with the
        prior's id and the ids of the Capabilities it governs.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """Move `GOVERNED_BY` edges to the approved fork and cascade its status, as one guarded group.

    A gateway read-resolve-write. In order: the graph must be caught up; the log position P0 is
    read; the prior's governed set is re-read and compared with `capability_ids` in Python (a set
    that changed since the caller's read returns `False` with NOTHING logged, not even a legacy-edge
    stamp); then legacy edges are stamped (a group with no audit link, because `GOVERNED_BY` edges
    written before the gateway have no identity to delete by); finally the group is submitted with
    `ExpectedPosition(P0 + stamped)`. A write that landed between the position read and the submit
    trips that precondition and also returns `False`. The position guard is coarser than a
    statement-level guard: an unrelated write in that window also refuses the move.

    The group deletes each `GOVERNED_BY` edge from the prior, upserts it from the same Capability to
    the fork, and cascades `target_status` over the fork's tree, so a Capability never has zero or
    two governors.

    Args:
        graph: The single-tenant policy graph handle, used for the guard's reads.
        target: The gateway target of the single-tenant policy graph.
        policy_id: The fork being approved, the new governor.
        prior_id: The superseded Policy that currently governs the Capabilities.
        capability_ids: The Capabilities read from `prior_id` beforehand.
        target_status: The status to cascade over the fork's tree.
        audit_event_id: The id of the `applied` approval audit row; the group is linked to it.

    Returns:
        `True` when the move and cascade were applied; `False` when the guard failed (the governance
        changed or the log moved), in which case nothing of this group was written.

    Raises:
        redis.exceptions.RedisError: a guard read failed.
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the graph is behind its
            log, the log position could not be read, or the gateway refused or failed the stamp or
            the group.

    Side Effects:
        Reads the graph and the log position and updates `dependency_health`. May submit a stamp
        group with no audit link (it changes no data), then one gateway group linked to
        `audit_event_id`.
        Returns `False` without submitting the move when the guard fails.
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
    stamped = ensure_stamped(target, None)  # housekeeping stamp: no audit link, no data change
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
    """One Control child to mint alongside a `create_policy_draft` call.

    This module writes graph structure only; it never derives identity.

    Attributes:
        id: The Control id, already computed by the caller (`domain_mapper.identity.control_id`).
        title: The Control's title.
        control_type: The Control's `type` value, for example `"automated"` or `"manual"`.
        extra_properties: Additional content fields written with `title`/`type`/`status`, used by a
            supersede fork to carry a forked Control's full content across (`description`,
            `implementation_status`, and so on). Defaults to empty. These are written first, so a
            stray `title`, `type` or `status` among them never wins.
    """

    id: str
    title: str
    control_type: str
    extra_properties: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class StandardDraft:
    """One Standard child, with its own optional Control children, to mint.

    Attributes:
        id: The Standard id, already computed by the caller (`domain_mapper.identity.standard_id`).
        title: The Standard's title.
        controls: The Control children to mint under it; empty by default.
        extra_properties: Additional content fields, with the same purpose and precedence as
            `ControlDraft.extra_properties`, one level up. Defaults to empty.
    """

    id: str
    title: str
    controls: tuple[ControlDraft, ...] = field(default_factory=tuple)
    extra_properties: Mapping[str, object] = field(default_factory=dict[str, object])


def find_existing_policy(graph: GraphHandle, policy_id: str) -> tuple[str, str] | None:
    """Check whether a Policy with `policy_id` already exists.

    A pure read, so a title-collision check still counts towards `/ready`'s FalkorDB connectivity
    signal.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The `policy_id(title)` the caller is about to mint.

    Returns:
        `(existing_id, existing_title)` when a `Policy` node with this id exists, else `None`.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """Read which Policy, if any, currently governs each of `capability_ids`.

    A pure read. A Capability that does not exist (a cleanup merge deletes the one it absorbs) is
    absent from the result; an existing but ungoverned one maps to `None`.

    Args:
        graph: The single-tenant policy graph handle.
        capability_ids: The Capability ids a fresh draft wants to claim; may be empty.

    Returns:
        `{capability_id: governing_policy_id | None}` for every id that exists.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
    """
    result = _read_query(
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
    """Mint a new draft Policy, plus any Standard/Control children, as ONE gateway group.

    This variant makes no Capability claim; `create_claimed_policy_draft` is the one that does. The
    group, in order, is: an `UpsertNode` for the Policy (`status="draft"`, `version`, the owner
    pair), the optional `SUPERSEDED_BY:{prior}->{new}` edge, then for each Standard an `UpsertNode`
    and its `SUPPORTED_BY:{policy}->{standard}` edge and for each of its Controls an `UpsertNode`
    and its `IMPLEMENTED_BY:{standard}->{control}` edge. The group is atomic: a failure leaves no
    partial draft.

    Business Rules: every child is written with `status="draft"` unconditionally. A child's
    `extra_properties` (when forking, the source tree's full content minus `id`/`title`/`status`)
    are spread BEFORE `title`/`type`/`status`, so a stray copied value can never win. The supersede
    link is a single Policy-level edge: there are no per-Standard or per-Control lineage edges. A
    value the gateway cannot store (a reserved key such as `embedding`, an empty list) is refused
    before anything is written.

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner: The creating actor's `(subject, issuer)` pair.
        standards: Optional Standard children, each with its own optional Control children; ids
            already computed by the caller.
        supersedes_policy_id: The prior Policy id this draft amends through `SUPERSEDED_BY`, or
            `None` for an ordinary (non-fork) draft.
        version: The new Policy's `version`, kept as a string: `"1"` for an ordinary draft, or
            `str(int(prior_version) + 1)` for a fork (computed by the caller).
        audit_event_id: The id of the `applied` `policy.create_draft` audit row the group is linked
            to.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (including a value it cannot store), could not reach its stores, or the group was
            committed but not applied.

    Side Effects:
        Submits one gateway group linked to `audit_event_id` (stamping the graph's legacy edges
        first if this target has not yet been stamped). Nothing is written if the group is refused.
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
    """Build a create's primitives: the Policy, its optional supersede edge, then the children."""
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
    """Mint a draft Policy that CLAIMS `capability_ids`, as one guarded gateway group.

    In order: the graph must be caught up; the log position P0 is read; `read_capability_governors`
    re-resolves every claimed Capability in Python (it exists and has no governing Policy). A claim
    that no longer holds returns `False` with NOTHING logged, not even a legacy-edge stamp. Only
    then are the graph's legacy edges stamped (a group with no audit link) and the group submitted
    with `ExpectedPosition(P0 + stamped)`. A write that landed
    between the position read and the submit trips that precondition and also returns `False`, with
    nothing of this group logged (a stamp group ahead of it carries no audit link and changes no
    data, and may stay in the log). The position guard is coarser than a statement-level guard: an
    unrelated write in that window also refuses the claim.

    The group holds an `UpsertNode` for the Policy, a `GOVERNED_BY:{capability}->{policy}`
    `UpsertEdge` per Capability, and the Standard/Control children as `create_policy_draft` writes
    them. A claim is always a fresh `version` "1" draft, never combined with a supersede fork.

    Args:
        graph: The single-tenant policy graph handle, used for the guard's reads.
        target: The gateway target of the single-tenant policy graph.
        policy_id: The already-computed, collision-checked new Policy id.
        title: The Policy's title.
        owner: The creating actor's `(subject, issuer)` pair.
        standards: Optional Standard children, each with its own optional Control children.
        capability_ids: The Capabilities the draft claims, de-duplicated and order-preserving.
        audit_event_id: The id of the `applied` `policy.create_draft` audit row the group is linked
            to.

    Returns:
        `True` when the draft was written; `False` when the claim no longer held (a Capability was
        governed, merged or removed after the caller's pre-check, or the log position moved), in
        which case no Policy, edge, Standard or Control was written.

    Raises:
        redis.exceptions.RedisError: the guard read failed.
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the graph is behind its
            log, the log position could not be read, a value could not be stored, or the gateway
            refused or failed the stamp or the group.

    Side Effects:
        Reads the graph and the log position and updates `dependency_health`. May submit a stamp
        group with no audit link, then one gateway group linked to `audit_event_id`. Returns
        `False` without submitting the draft when the guard fails.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    governors = read_capability_governors(graph, capability_ids)
    if any(governors.get(capability_id, "") is not None for capability_id in capability_ids):
        return False
    stamped = ensure_stamped(target, None)  # housekeeping stamp: no audit link, no data change
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
    """One Control read back off an existing Policy's tree.

    Attributes:
        id: The Control id.
        title: The Control's title.
        status: The Control's governance status.
        control_type: The Control's `type` value.
    """

    id: str
    title: str
    status: str
    control_type: str


@dataclass(frozen=True, slots=True)
class StandardRecord:
    """One Standard, with its Control children, read back off an existing Policy's tree.

    Attributes:
        id: The Standard id.
        title: The Standard's title.
        status: The Standard's governance status.
        controls: Its Control children, in read order; empty when it has none.
    """

    id: str
    title: str
    status: str
    controls: tuple[ControlRecord, ...]


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """A Policy's own fields plus its full Standard/Control tree.

    Attributes:
        id: The Policy id.
        title: The Policy's title.
        status: The Policy's governance status.
        version: The Policy's version, a string.
        owner_subject: The owner's token subject.
        owner_issuer: The owner's token issuer; the pair `(owner_subject, owner_issuer)` identifies
            the owner.
        standards: Its Standard children, in read order; empty when it has none.
    """

    id: str
    title: str
    status: str
    version: str
    owner_subject: str
    owner_issuer: str
    standards: tuple[StandardRecord, ...]


@dataclass
class _StandardAccumulator:
    """Mutable per-Standard accumulator used only while grouping `read_policy_tree`'s rows."""

    title: str
    status: str
    controls: list[ControlRecord] = field(default_factory=list)


def read_policy_tree(graph: GraphHandle, policy_id: str) -> PolicyRecord | None:
    """Read `policy_id`'s own fields plus its full Standard/Control tree.

    A pure read. Callers should run `backfill_governance_status` first so `version` and `status` are
    never `NULL` here. One Cypher statement walks the two-hop `Policy -[:SUPPORTED_BY]-> Standard
    -[:IMPLEMENTED_BY]-> Control` path with `OPTIONAL MATCH`, so a Policy with zero Standards (or a
    Standard with zero Controls) still returns one row for the Policy with `NULL` Standard/Control
    columns; the rows are grouped back into a nested `PolicyRecord`.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The Policy id to read.

    Returns:
        A `PolicyRecord` with its `standards` (each with their `controls`) in the order FalkorDB
        returned them, or `None` when no `Policy` node has this id.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """One Control's full content, read for a supersede fork.

    Deliberately not `ControlRecord`, which carries only id, title, status and type: forking needs
    every content field (`description`, `implementation_status`, `execution_frequency`, ...).

    Attributes:
        title: The Control's title (empty string when the node has none).
        properties: The Control node's full property map as FalkorDB's `properties()` returns it,
            including `id`, `title`, `status` and `type`. The fork builder strips those four keys
            before reusing the rest as a new Control's `extra_properties`, because a forked node
            always gets its own freshly computed id and status.
    """

    title: str
    properties: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ForkedStandardRecord:
    """One Standard's full content, plus its Control children, read for a supersede fork.

    Same rationale as `ForkedControlRecord`, one level up.

    Attributes:
        title: The Standard's title (empty string when the node has none).
        properties: The Standard node's full property map as FalkorDB's `properties()` returns it.
        controls: Its Control children with their full content, in read order; empty when it has
            none.
    """

    title: str
    properties: Mapping[str, object]
    controls: tuple[ForkedControlRecord, ...]


@dataclass
class _ForkedStandardAccumulator:
    """Mutable per-Standard accumulator for grouping `read_policy_tree_for_fork`'s rows."""

    title: str
    properties: Mapping[str, object]
    controls: list[ForkedControlRecord] = field(default_factory=list)


def read_policy_tree_for_fork(
    graph: GraphHandle, policy_id: str
) -> tuple[ForkedStandardRecord, ...]:
    """Read the full content of `policy_id`'s Standard/Control tree, for a supersede fork.

    A pure read, separate from `read_policy_tree`: that function's records carry only id, title and
    status (plus Control type), enough for a Policy view but not for forking real content
    (`description`, `procedure`, `execution_frequency`, ...) into a new draft. The single Cypher
    statement walks the same `SUPPORTED_BY`/`IMPLEMENTED_BY` path with `OPTIONAL MATCH` but returns
    each node's full `properties()` map. `s.id` and `c.id` are returned alongside each map only as
    stable scalar keys for the Python-side grouping by Standard.

    Callers should already have confirmed that `policy_id` is `"approved"`; this function does not
    gate on status.

    Args:
        graph: The single-tenant policy graph handle.
        policy_id: The Policy id whose current Standard/Control tree is forked.

    Returns:
        Every Standard under `policy_id`, each with its Control children, in the order FalkorDB
        returned them. A Policy with no Standards (or an unknown id) returns `()`.

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """PATCH a subset of an existing Policy's own content fields.

    An explicit `None` value in `properties` means "clear this field"; a key that is absent leaves
    the existing value untouched (the caller includes only keys it means to change). The patch is
    ONE gateway group with no audit link (a draft edit writes no `audit_events` row): a
    `MergeProperty` for every non-`None` value, then a `RemoveProperty` for every cleared key. A
    patch that sets nothing has no merge, one that clears nothing has no remove, and a patch that
    changes nothing logs nothing.

    Property key names are never free caller strings here: `properties` is the already-validated
    output of the MCP boundary's patch-field parser, whose keys come from a fixed, code-defined
    allow-list derived from `ps_service.domain_schema`; the gateway validates each key again against
    its identifier pattern before it reaches Cypher.

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The existing Policy id to patch.
        properties: Field name to new value (or `None` to clear), already restricted to the allowed
            patchable-field set by the caller.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (for example a value it cannot store), could not reach its stores, or the group
            was committed but not applied.

    Side Effects:
        Submits one gateway group with no audit link (stamping the graph's legacy edges first if
        this target has not yet been stamped); nothing is submitted for an empty patch.
    """
    submit_group(
        target,
        _patch_primitives("Policy", policy_id, properties),
        audit_event_id=None,
    )


def _patch_primitives(
    label: str, node_id: str, properties: Mapping[str, object]
) -> tuple[Primitive, ...]:
    """Build one PATCH's primitives: a merge of the set values, then a remove of cleared keys."""
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
    """Build the primitives that mint a child under `parent`: the node, then the edge to it."""
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
    """Mint a new Standard under an existing draft Policy.

    Unlike a patch, a newly minted node has no prior value to clear: an explicit `None` in
    `extra_properties` (for example `fields={"description": None}` from the add-standard tool) means
    "don't set this field", and `None`-valued keys are filtered out.

    Business Rules: `status` is always forced to `"draft"`, whatever `extra_properties` holds.
    `implementation_status` defaults to `"draft"` unless the caller supplied it (already validated
    at the MCP boundary). `title` and `status` are set AFTER `extra_properties`, so a
    caller-supplied value can never override them. The Standard's id derives from its parent and
    title (`domain_mapper.identity.standard_id`), so adding the same Standard twice under one
    parent changes nothing the second time.

    The write is ONE gateway group with no audit link: an `UpsertNode` for the Standard, then the
    `UpsertEdge` `SUPPORTED_BY:{policy}->{standard}` from its parent.

    Args:
        target: The gateway target of the single-tenant policy graph.
        policy_id: The existing draft Policy id to attach the Standard to.
        standard_id: The already-computed new Standard id
            (`domain_mapper.identity.standard_id(policy_id, title)`).
        title: The new Standard's title.
        extra_properties: Additional patchable content fields the caller supplied at creation time,
            already restricted to the Standard's patchable fields by the MCP boundary; `None`-valued
            keys are dropped, never written.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (including a value it cannot store), could not reach its stores, or the group was
            committed but not applied.

    Side Effects:
        Submits one gateway group with no audit link (stamping the graph's legacy edges first if
        this target has not yet been stamped).
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
    """A Standard's own status and title plus its parent Policy's owner and status.

    A Standard has no owner of its own: ownership is derived at query time by a one-hop traversal to
    the parent Policy, never a stored field. This row is that traversal's full result, so the
    service can build a `PolicyLifecycleRuleContext` (owner pair plus the Standard's own status,
    the node being mutated, never the parent Policy's status) without a second read.

    Attributes:
        policy_id: The parent Policy's id.
        policy_owner_subject: The parent Policy owner's token subject.
        policy_owner_issuer: The parent Policy owner's token issuer.
        policy_status: The parent Policy's status.
        standard_status: The Standard's own status.
        standard_title: The Standard's title.
    """

    policy_id: str
    policy_owner_subject: str
    policy_owner_issuer: str
    policy_status: str
    standard_status: str
    standard_title: str


def find_standard_with_parent(graph: GraphHandle, standard_id: str) -> StandardWithParent | None:
    """Read an existing Standard with its parent Policy's owner and status (one hop).

    A pure read. Callers should backfill the parent Policy's tree first so `standard_status` is
    never spuriously `NULL` when a draft-status gate inspects it; this function does not backfill.

    Args:
        graph: The single-tenant policy graph handle.
        standard_id: The Standard id to read.

    Returns:
        A `StandardWithParent` row, or `None` when no `Policy -[:SUPPORTED_BY]-> Standard {id:
        standard_id}` path exists (the id is unknown, or the Standard has no parent Policy; both are
        "not found", since every real Standard has exactly one parent).

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """PATCH a subset of an existing Standard's own content fields.

    Same split as `update_policy_fields`: one gateway group with no audit link, a `MergeProperty`
    for every non-`None` value then a `RemoveProperty` for every cleared key; an explicit `None`
    clears a field, an absent key leaves it, and a patch that changes nothing logs nothing.
    `properties` is the already-validated output of the MCP boundary's patch-field parser, whose
    keys come from the code-defined allow-list of the Standard's patchable fields (derived from
    `ps_service.domain_schema`).

    Args:
        target: The gateway target of the single-tenant policy graph.
        standard_id: The existing Standard id to patch.
        properties: Field name to new value (or `None` to clear), already restricted to the allowed
            patchable-field set by the caller.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (for example a value it cannot store), could not reach its stores, or the group
            was committed but not applied.

    Side Effects:
        Submits one gateway group with no audit link (stamping the graph's legacy edges first if
        this target has not yet been stamped); nothing is submitted for an empty patch.
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
    """Mint a new Control under an existing draft Standard.

    Mirrors `add_standard_to_policy` one level deeper. Ownership and status gating for the call is
    the service's concern (it uses `find_standard_with_parent`); this function issues no read.

    Business Rules: `None`-valued keys of `extra_properties` are dropped. `status` is always forced
    to `"draft"`. `implementation_status` defaults to `"planned"` (not `"draft"` as for a Standard)
    unless the caller supplied one. `type` is always set from `control_type`, never from
    `extra_properties`. `title`, `type` and `status` are set AFTER `extra_properties`, so a
    caller-supplied value can never override them.

    The write is ONE gateway group with no audit link: an `UpsertNode` for the Control, then the
    `UpsertEdge` `IMPLEMENTED_BY:{standard}->{control}` from its parent.

    Args:
        target: The gateway target of the single-tenant policy graph.
        standard_id: The existing draft Standard id to attach the Control to.
        control_id: The already-computed new Control id
            (`domain_mapper.identity.control_id(standard_id, title)`).
        title: The new Control's title.
        control_type: The new Control's `type` (`"automated"` or `"manual"`), already validated at
            the MCP boundary.
        extra_properties: Additional patchable content fields the caller supplied at creation time,
            already restricted to the Control's patchable fields other than `type` by the MCP
            boundary; `None`-valued keys are dropped, never written.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (including a value it cannot store), could not reach its stores, or the group was
            committed but not applied.

    Side Effects:
        Submits one gateway group with no audit link (stamping the graph's legacy edges first if
        this target has not yet been stamped).
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
    """A Control's own status and title plus its root Policy's owner and status.

    Updating a Control is the one genuinely two-hop transitive-ownership operation
    (`Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control`), unlike the Standard-level
    operations that need one hop. Neither the Control nor its parent Standard has an owner of its
    own: ownership is derived by walking to the root Policy. The parent Standard's id is carried
    too, so the row is the full traversal result and needs no second read.

    Attributes:
        standard_id: The parent Standard's id.
        policy_id: The root Policy's id.
        policy_owner_subject: The root Policy owner's token subject.
        policy_owner_issuer: The root Policy owner's token issuer.
        policy_status: The root Policy's status.
        control_status: The Control's own status.
        control_title: The Control's title.
    """

    standard_id: str
    policy_id: str
    policy_owner_subject: str
    policy_owner_issuer: str
    policy_status: str
    control_status: str
    control_title: str


def find_control_with_parent(graph: GraphHandle, control_id: str) -> ControlWithParent | None:
    """Read an existing Control with its parent Standard and root Policy's owner and status.

    A pure read. Callers should backfill the root Policy's tree first so `control_status` is never
    spuriously `NULL` when a draft-status gate inspects it; this function does not backfill.

    Args:
        graph: The single-tenant policy graph handle.
        control_id: The Control id to read.

    Returns:
        A `ControlWithParent` row, or `None` when no `Policy -[:SUPPORTED_BY]-> Standard
        -[:IMPLEMENTED_BY]-> Control {id: control_id}` path exists (the id is unknown, or its parent
        chain is broken; both are "not found", since every real Control has exactly one parent
        Standard and one root Policy).

    Raises:
        redis.exceptions.RedisError: the read failed (the dependency is also marked unhealthy).

    Side Effects:
        Reads the graph and updates `dependency_health`; nothing is written.
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
    """PATCH a subset of an existing Control's own content fields.

    Behaves like `update_standard_fields` for a Control: one gateway group with no audit link, a
    `MergeProperty` for every non-`None` value then a `RemoveProperty` for every cleared key; an
    explicit `None` clears a field, an absent key leaves it, and a patch that changes nothing logs
    nothing. `properties` keys come from the code-defined allow-list of the Control's patchable
    fields.

    Args:
        target: The gateway target of the single-tenant policy graph.
        control_id: The existing Control id to patch.
        properties: Field name to new value (or `None` to clear), already restricted to the allowed
            patchable-field set by the caller.

    Returns:
        None.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the gateway refused the
            group (for example a value it cannot store), could not reach its stores, or the group
            was committed but not applied.

    Side Effects:
        Submits one gateway group with no audit link (stamping the graph's legacy edges first if
        this target has not yet been stamped); nothing is submitted for an empty patch.
    """
    submit_group(target, _patch_primitives("Control", control_id, properties), audit_event_id=None)
