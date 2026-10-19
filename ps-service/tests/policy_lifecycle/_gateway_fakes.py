"""The Graph Write Gateway rig, seen from Policy Lifecycle.

Policy Lifecycle submits its graph writes as gateway groups and only reads the graph through a
handle. `LifecycleRig` is the gateway rig (in-memory log and FalkorDB fakes, the true boundaries)
plus the `policy_system` graph, a read-only `PolicyReadGraph` over it, an audit store that hands
out event ids registered with the rig's log, and a seeding API for fixture Policy trees.

`PolicyReadGraph` answers the fixed read statements `graph_writer`'s `find_*`/`read_*` functions
issue, from the rig's own nodes and edges, so it sees every node a gateway group wrote; it refuses
any statement that carries a write keyword (there is no legacy write path left in a lifecycle test).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from audit._fakes import InMemoryAuditStore
from graph_gateway._fakes import FakeQueryResult, GatewayRig, InMemoryGraph

from ps_service.authz.models import AccessRole
from ps_service.authz.service import check_role
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.policy_lifecycle import audit_actions

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.authz.store import AccessRoleStore
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GroupOutcome
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter

POLICY_SYSTEM = "policy_system"
_REGISTERED_ACTIONS = (
    audit_actions.PolicyCreateDraftDetails,
    audit_actions.PolicyTransitionDetails,
)
"""Importing `audit_actions` registers the lifecycle actions the audit double validates against."""
DEFAULT_OWNER = ("owner-sub", "https://issuer.example")

_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DETACH|DROP)\b")

_FIND_APPROVED_PRIOR = (
    "MATCH (prior:Policy)-[:SUPERSEDED_BY]->(:Policy {id: $successor_policy_id}) "
    "WHERE prior.status = 'approved' "
    "RETURN prior.id LIMIT 1"
)
_READ_FORK_GOVERNANCE = (
    "MATCH (prior:Policy)-[:SUPERSEDED_BY]->(:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (cap:Capability)-[:GOVERNED_BY]->(prior) "
    "RETURN prior.id, collect(cap.id) LIMIT 1"
)
_FIND_EXISTING_POLICY = "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title"
_READ_CAPABILITY_GOVERNORS = (
    "MATCH (cap:Capability) WHERE cap.id IN $capability_ids "
    "OPTIONAL MATCH (cap)-[:GOVERNED_BY]->(g:Policy) "
    "RETURN cap.id, g.id"
)
_READ_POLICY_TREE = (
    "MATCH (p:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
    "RETURN p.id, p.title, p.status, p.version, p.owner_subject, p.owner_issuer, "
    "s.id, s.title, s.status, c.id, c.title, c.status, c.type"
)
_READ_POLICY_TREE_FOR_FORK = (
    "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
    "RETURN s.id, properties(s), c.id, properties(c)"
)
_READ_BACKFILL_STATE = (
    "MATCH (p:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
    "RETURN p.version, p.status, s.id, s.status, c.id, c.status"
)
_FIND_STANDARD_WITH_PARENT = (
    "MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
    "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title"
)
_FIND_CONTROL_WITH_PARENT = (
    "MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->"
    "(c:Control {id: $control_id}) "
    "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title"
)


class PolicyReadGraph:
    """A read-only `GraphHandle` over a rig graph: the lifecycle's fixed reads, no writes.

    A statement it was not taught raises, and so does one carrying a write keyword, so a write
    that reached it would be a production write path that skipped the gateway.
    """

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph
        self.statements: list[str] = []
        """Every statement received, in order (for the tests of this double itself)."""
        self._reads: dict[str, Callable[[dict[str, object]], list[object]]] = {
            _FIND_APPROVED_PRIOR: self._find_approved_prior,
            _READ_FORK_GOVERNANCE: self._read_fork_governance,
            _FIND_EXISTING_POLICY: self._find_existing_policy,
            _READ_CAPABILITY_GOVERNORS: self._read_capability_governors,
            _READ_POLICY_TREE: self._read_policy_tree,
            _READ_POLICY_TREE_FOR_FORK: self._read_policy_tree_for_fork,
            _READ_BACKFILL_STATE: self._read_backfill_state,
            _FIND_STANDARD_WITH_PARENT: self._find_standard_with_parent,
            _FIND_CONTROL_WITH_PARENT: self._find_control_with_parent,
        }

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        """Answer one fixed read statement from the rig graph (`timeout` is accepted, ignored)."""
        del timeout
        self.statements.append(q)
        if _WRITE_KEYWORD.search(q):
            raise AssertionError(f"the lifecycle read double refuses a write statement: {q}")
        read = self._reads.get(q)
        if read is None:
            raise AssertionError(f"not a lifecycle read statement: {q}")
        return FakeQueryResult(read(params or {}))

    # -- graph navigation ----------------------------------------------------------------------

    def _node(self, label: str, node_id: str) -> dict[str, object] | None:
        return self._graph.nodes.get((label, node_id))

    def _targets(self, edge_type: str, source: tuple[str, str], target_label: str) -> list[str]:
        """Ids of `target_label` nodes `source` points at by `edge_type`, in insertion order."""
        seen: list[str] = []
        for key in self._graph.edges:
            if key[0] == edge_type and (key[1], key[2]) == source and key[3] == target_label:
                seen.append(key[4])
        return seen

    def _sources(self, edge_type: str, source_label: str, target: tuple[str, str]) -> list[str]:
        """Ids of `source_label` nodes that point at `target` by `edge_type`, in insertion order."""
        return [
            key[2]
            for key in self._graph.edges
            if key[0] == edge_type and key[1] == source_label and (key[3], key[4]) == target
        ]

    def _properties(self, label: str, node_id: str) -> dict[str, object]:
        """FalkorDB's `properties(n)`: the stored map, `id` included."""
        return {"id": node_id, **self._graph.nodes[(label, node_id)]}

    # -- the reads -----------------------------------------------------------------------------

    def _find_approved_prior(self, params: dict[str, object]) -> list[object]:
        successor = cast("str", params["successor_policy_id"])
        for prior_id in self._sources("SUPERSEDED_BY", "Policy", ("Policy", successor)):
            prior = self._node("Policy", prior_id)
            if prior is not None and prior.get("status") == "approved":
                return [[prior_id]]
        return []

    def _read_fork_governance(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        for prior_id in self._sources("SUPERSEDED_BY", "Policy", ("Policy", policy_id)):
            return [[prior_id, self._sources("GOVERNED_BY", "Capability", ("Policy", prior_id))]]
        return []

    def _find_existing_policy(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        policy = self._node("Policy", policy_id)
        return [] if policy is None else [[policy_id, policy.get("title")]]

    def _read_capability_governors(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["capability_ids"]))
        rows: list[object] = []
        for label, node_id in self._graph.nodes:
            if label != "Capability" or node_id not in wanted:
                continue
            governors = self._targets("GOVERNED_BY", ("Capability", node_id), "Policy")
            rows.extend([node_id, governor] for governor in governors)
            if not governors:
                rows.append([node_id, None])
        return rows

    def _tree_rows(self, policy_id: str) -> list[tuple[str | None, str | None]]:
        """The `OPTIONAL MATCH` rows of a Policy's tree: `(standard id|None, control id|None)`."""
        standards = self._targets("SUPPORTED_BY", ("Policy", policy_id), "Standard")
        if not standards:
            return [(None, None)]
        rows: list[tuple[str | None, str | None]] = []
        for standard_id in standards:
            controls = self._targets("IMPLEMENTED_BY", ("Standard", standard_id), "Control")
            rows.extend((standard_id, control_id) for control_id in controls)
            if not controls:
                rows.append((standard_id, None))
        return rows

    def _read_policy_tree(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        policy = self._node("Policy", policy_id)
        if policy is None:
            return []
        head = [
            policy_id,
            policy.get("title"),
            policy.get("status"),
            policy.get("version"),
            policy.get("owner_subject"),
            policy.get("owner_issuer"),
        ]
        rows: list[object] = []
        for standard_id, control_id in self._tree_rows(policy_id):
            standard = None if standard_id is None else self._node("Standard", standard_id)
            control = None if control_id is None else self._node("Control", control_id)
            rows.append(
                [
                    *head,
                    standard_id,
                    None if standard is None else standard.get("title"),
                    None if standard is None else standard.get("status"),
                    control_id,
                    None if control is None else control.get("title"),
                    None if control is None else control.get("status"),
                    None if control is None else control.get("type"),
                ]
            )
        return rows

    def _read_backfill_state(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        policy = self._node("Policy", policy_id)
        if policy is None:
            return []
        rows: list[object] = []
        for standard_id, control_id in self._tree_rows(policy_id):
            standard = None if standard_id is None else self._node("Standard", standard_id)
            control = None if control_id is None else self._node("Control", control_id)
            rows.append(
                [
                    policy.get("version"),
                    policy.get("status"),
                    standard_id,
                    None if standard is None else standard.get("status"),
                    control_id,
                    None if control is None else control.get("status"),
                ]
            )
        return rows

    def _read_policy_tree_for_fork(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        if self._node("Policy", policy_id) is None:
            return []
        rows: list[object] = []
        for standard_id, control_id in self._tree_rows(policy_id):
            if standard_id is None:
                continue  # MATCH (p)-[:SUPPORTED_BY]->(s) is not optional
            rows.append(
                [
                    standard_id,
                    self._properties("Standard", standard_id),
                    control_id,
                    None if control_id is None else self._properties("Control", control_id),
                ]
            )
        return rows

    def _find_standard_with_parent(self, params: dict[str, object]) -> list[object]:
        standard_id = cast("str", params["standard_id"])
        standard = self._node("Standard", standard_id)
        rows: list[object] = []
        for policy_id in self._sources("SUPPORTED_BY", "Policy", ("Standard", standard_id)):
            policy = self._node("Policy", policy_id)
            if policy is None or standard is None:
                continue
            rows.append(
                [
                    policy_id,
                    policy.get("owner_subject"),
                    policy.get("owner_issuer"),
                    policy.get("status"),
                    standard.get("status"),
                    standard.get("title"),
                ]
            )
        return rows

    def _find_control_with_parent(self, params: dict[str, object]) -> list[object]:
        control_id = cast("str", params["control_id"])
        control = self._node("Control", control_id)
        rows: list[object] = []
        for standard_id in self._sources("IMPLEMENTED_BY", "Standard", ("Control", control_id)):
            for policy_id in self._sources("SUPPORTED_BY", "Policy", ("Standard", standard_id)):
                policy = self._node("Policy", policy_id)
                if policy is None or control is None:
                    continue
                rows.append(
                    [
                        standard_id,
                        policy_id,
                        policy.get("owner_subject"),
                        policy.get("owner_issuer"),
                        policy.get("status"),
                        control.get("status"),
                        control.get("title"),
                    ]
                )
        return rows


class FlippingRead(PolicyReadGraph):
    """A read double that changes the graph just before the Nth read of one statement kind.

    The test names the statement by a distinctive `marker` substring: the first matching read is
    the service's own, and the change lands just before a later one -- "the graph moved between
    the read and the write-time re-read", with no write path of its own.
    """

    def __init__(
        self, graph: InMemoryGraph, flip: Callable[[], None], *, marker: str, nth: int = 2
    ) -> None:
        super().__init__(graph)
        self._flip = flip
        self._marker = marker
        self._nth = nth
        self._marker_reads = 0

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        """Flip the graph once, just before the Nth read whose text contains the marker."""
        if self._marker in q:
            self._marker_reads += 1
            if self._marker_reads == self._nth:
                self._flip()
        return super().query(q, params, timeout)


class EventIdFakeAuditStore(InMemoryAuditStore):
    """The registry-validating in-memory audit store, handing out event ids (`EventIdAuditStore`).

    `LifecycleRig` points `event_id_sink` at the rig's log, so a group anchored to an `applied`
    row this store wrote is accepted (the fake log checks the foreign key).
    """


@dataclass(frozen=True)
class SeedControl:
    """One Control of a fixture tree. `status=None` seeds a legacy node with no `status`."""

    id: str
    title: str
    status: str | None = "draft"
    control_type: str = "manual"
    extra: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True)
class SeedStandard:
    """One Standard of a fixture tree. `status=None` seeds a legacy node with no `status`."""

    id: str
    title: str
    status: str | None = "draft"
    controls: tuple[SeedControl, ...] = ()
    extra: Mapping[str, object] = field(default_factory=dict[str, object])


class PolicyGraphSeeder:
    """Seeds fixture Policy trees into a rig graph as a pre-gateway writer left them.

    A mixin over a `graph` attribute: `LifecycleRig` is one, and a test that is handed a process
    gateway rig (the MCP tool tests) wraps that rig's `policy_system` graph in one.
    """

    graph: InMemoryGraph

    def __init__(self, graph: InMemoryGraph) -> None:
        """Seed into `graph`."""
        self.graph = graph

    def _seed_edge(
        self,
        edge: tuple[str, str, str, str, str],
        *,
        legacy: bool,
        properties: Mapping[str, object] | None = None,
    ) -> None:
        if legacy:
            self.graph.seed_legacy_edge(edge, dict(properties or {}))
            return
        identity = f"{edge[0]}:{edge[2]}->{edge[4]}"
        key = (*edge, identity)
        self.graph.edges[key] = {"identity": identity, **(properties or {})}
        self.graph.edge_internal_id(key)

    def seed_policy(
        self,
        policy_id: str,
        *,
        title: str | None = None,
        status: str | None = "draft",
        version: str | None = "1",
        owner: tuple[str, str] = DEFAULT_OWNER,
        standards: tuple[SeedStandard, ...] = (),
        legacy_edges: bool = True,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """Seed a Policy with its Standard/Control tree.

        `status=None` / `version=None` seed a legacy node lacking the property (a backfill
        fixture). `legacy_edges` seeds the tree's edges without a gateway `identity`.
        """
        properties: dict[str, object] = {
            "title": title or policy_id,
            "owner_subject": owner[0],
            "owner_issuer": owner[1],
            **(extra or {}),
        }
        if status is not None:
            properties["status"] = status
        if version is not None:
            properties["version"] = version
        self.graph.seed_node(("Policy",), policy_id, properties)
        for standard in standards:
            standard_properties: dict[str, object] = {"title": standard.title, **standard.extra}
            if standard.status is not None:
                standard_properties["status"] = standard.status
            self.graph.seed_node(("Standard",), standard.id, standard_properties)
            self._seed_edge(
                ("SUPPORTED_BY", "Policy", policy_id, "Standard", standard.id), legacy=legacy_edges
            )
            for control in standard.controls:
                control_properties: dict[str, object] = {
                    "title": control.title,
                    "type": control.control_type,
                    **control.extra,
                }
                if control.status is not None:
                    control_properties["status"] = control.status
                self.graph.seed_node(("Control",), control.id, control_properties)
                self._seed_edge(
                    ("IMPLEMENTED_BY", "Standard", standard.id, "Control", control.id),
                    legacy=legacy_edges,
                )

    def seed_standard_under(
        self, policy_id: str, standard: SeedStandard, *, legacy: bool = True
    ) -> None:
        """Seed one more Standard (with its Controls) under an already seeded Policy."""
        properties: dict[str, object] = {"title": standard.title, **standard.extra}
        if standard.status is not None:
            properties["status"] = standard.status
        self.graph.seed_node(("Standard",), standard.id, properties)
        self._seed_edge(
            ("SUPPORTED_BY", "Policy", policy_id, "Standard", standard.id), legacy=legacy
        )
        for control in standard.controls:
            control_properties: dict[str, object] = {
                "title": control.title,
                "type": control.control_type,
                **control.extra,
            }
            if control.status is not None:
                control_properties["status"] = control.status
            self.graph.seed_node(("Control",), control.id, control_properties)
            self._seed_edge(
                ("IMPLEMENTED_BY", "Standard", standard.id, "Control", control.id), legacy=legacy
            )

    def seed_capability(
        self, capability_id: str, *, name: str | None = None, status: str | None = None
    ) -> None:
        """Seed a Capability (`status=None` leaves it unset)."""
        properties: dict[str, object] = {"name": name or capability_id}
        if status is not None:
            properties["status"] = status
        self.graph.seed_node(("Capability",), capability_id, properties)

    def seed_governs(self, capability_id: str, policy_id: str, *, legacy: bool = True) -> None:
        """Seed a `(Capability)-[:GOVERNED_BY]->(Policy)` edge."""
        self._seed_edge(
            ("GOVERNED_BY", "Capability", capability_id, "Policy", policy_id), legacy=legacy
        )

    def seed_supersedes(self, prior_id: str, new_id: str, *, legacy: bool = True) -> None:
        """Seed a `(prior)-[:SUPERSEDED_BY]->(new)` edge."""
        self._seed_edge(("SUPERSEDED_BY", "Policy", prior_id, "Policy", new_id), legacy=legacy)


class HookedSubmitter:
    """The rig gateway with a one-shot hook just before the delegated `submit_group` (L6).

    `read_log_position` is not hooked, so a callback appended through the real gateway lands
    exactly between a guarded flow's position read and its submit: the stale-position window.
    """

    def __init__(self, gateway: LifecycleSubmitter) -> None:
        self._gateway = gateway
        self._before_submit: Callable[[], None] | None = None

    def before_submit(self, callback: Callable[[], None]) -> None:
        """Run `callback` once, just before the next delegated `submit_group`."""
        self._before_submit = callback

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Run the pending hook (once), then submit through the real gateway."""
        callback, self._before_submit = self._before_submit, None
        if callback is not None:
            callback()
        return self._gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        """Delegate."""
        return self._gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        """Delegate."""
        return self._gateway.stamp_legacy_edge_identity(graph, audit_event_id)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        """Delegate."""
        return self._gateway.read_persisted_elements(graph)

    def read_log_position(self, graph: str) -> int:
        """Delegate (never hooked)."""
        return self._gateway.read_log_position(graph)


class LifecycleRig(GatewayRig, PolicyGraphSeeder):
    """A gateway rig that also hands out `policy_system`, its read handle and an audit store."""

    def __init__(self) -> None:
        super().__init__()
        PolicyGraphSeeder.__init__(self, self.graphs.open(POLICY_SYSTEM))
        self.read = PolicyReadGraph(self.graph)
        self.audit = EventIdFakeAuditStore(
            events=self.events, event_id_sink=self.store.audit_rows.add
        )
        self.hooked = HookedSubmitter(self.gateway)
        """The gateway behind a one-shot `before_submit` hook (the stale-injection seam)."""

    def append_unrelated_group(self) -> None:
        """Append one group of an unrelated write through the real gateway (moves the position)."""
        self.gateway.submit_group(
            MutationGroup(
                graph=POLICY_SYSTEM,
                primitives=(UpsertNode(label="Capability", id="cap_unrelated", properties={}),),
            )
        )

    @property
    def single_tenant_graph_name(self) -> str:
        """The name the lifecycle writes under (`policy_system`)."""
        return POLICY_SYSTEM

    @property
    def logged_groups(self) -> int:
        """How many groups the log holds for `policy_system`."""
        return len([g for g in self.store.groups if g.graph == POLICY_SYSTEM])


def policy_manager_enforcer(store: AccessRoleStore) -> Callable[[tuple[str, str]], None]:
    """A service-direct `enforce_policy_manager`: a pure role check over `store`, no audit row.

    Production hands the service the request gate through the MCP tool; a test that calls
    `approve_policy` / `reject_policy` directly supplies this pure check instead.
    """
    return lambda actor: check_role(
        actor, minimum=AccessRole.POLICY_MANAGER, store=store
    ).raise_if_denied()
