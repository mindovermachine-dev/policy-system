# pyright: reportPrivateUsage=false
"""The Graph Write Gateway rig, seen from Graph Cleanup.

Graph Cleanup reads the graph through a handle and writes it only as gateway groups. `CleanupRig`
is the gateway rig (in-memory log and FalkorDB fakes, the true boundaries) plus the `policy_system`
graph, a read-only `CleanupReadGraph` over it, an audit store whose event ids are registered with
the rig's log, and a seeding API for fixture Capability/Policy graphs.

`CleanupReadGraph` answers the exact read statements `graph_reader` issues, from the rig's own
nodes and edges, so it sees every node a gateway group wrote. It refuses any statement carrying a
write keyword (there is no direct write path left in a cleanup test) and any read it was not
taught, so a new production read fails loudly here instead of returning nothing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import FakeQueryResult, GatewayRig, InMemoryGraph

from graph_cleanup._fakes import (
    ABSORBED,
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ID,
    SURVIVOR,
    RecordingAuditStore,
)
from ps_service.company_merge import pending_review
from ps_service.graph_cleanup import graph_reader
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.authz.store import AccessRoleStore
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.graph_submission import CleanupSubmitter
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GraphLogGroup, GroupOutcome

POLICY_SYSTEM = "policy_system"
OTHER_CAPABILITY = "cap_other"
POLICY = "pol_1"
"""The draft Policy `SURVIVOR` and `OTHER_CAPABILITY` are governed by in the default fixture."""

_ENDPOINT_LABELS = {"REQUIRES": "Obligation", "COVERS": "PracticeArea", "MITIGATED_BY": "RiskPath"}
"""The node label each capability-unmerge endpoint read looks for."""

_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DETACH|DROP)\b")


class CleanupReadGraph:
    """A read-only `GraphHandle` over a rig graph: Graph Cleanup's fixed reads, no writes."""

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph
        self.statements: list[str] = []
        """Every statement received, in order."""
        self._reads: dict[str, Callable[[dict[str, object]], list[object]]] = {
            graph_reader._CAPABILITY_NODES_QUERY: self._capability_nodes,
            graph_reader._CAPABILITY_GOVERNORS_QUERY: self._capability_governors,
            graph_reader._POLICY_GOVERNED_SETS_QUERY: self._policy_governed_sets,
            graph_reader._GOVERNED_EDGE_QUERY: self._governed_edge_count,
            graph_reader._CAPABILITY_MARKER_QUERY: self._capability_marker,
            graph_reader._CAPABILITY_ANY_MARKER_QUERY: self._capability_any_marker,
            graph_reader._CAPABILITY_PRESENT_QUERY: self._capability_present,
            graph_reader._OBLIGATION_NODES_QUERY: self._obligation_nodes,
            graph_reader._OBLIGATION_ROLES_QUERY: self._obligation_roles,
            graph_reader._OBLIGATION_SATISFIED_QUERY: self._obligation_satisfied,
            graph_reader._OBLIGATION_REQUIRES_QUERY: self._obligation_requires,
            graph_reader._OBLIGATION_REFS_QUERY: self._obligation_refs,
            graph_reader._OBLIGATION_MARKER_QUERY: self._obligation_marker,
            graph_reader._OBLIGATION_ANY_MARKER_QUERY: self._obligation_any_marker,
            graph_reader._OBLIGATION_PRESENT_QUERY: self._obligation_present,
            graph_reader._CAPABILITY_MARKERS_QUERY: self._capability_markers,
            graph_reader._POLICY_EXISTS_QUERY: self._policy_exists,
            graph_reader._OBLIGATION_MARKERS_QUERY: self._obligation_markers,
            graph_reader._ROLE_EXISTS_QUERY: self._role_exists,
            graph_reader._REQUIREMENTS_EXIST_QUERY: self._requirements_exist,
            graph_reader._CAPABILITIES_EXIST_QUERY: self._capabilities_exist,
            pending_review._REVIEW_MERGE_SIDES_QUERY: self._review_merge_sides,
            pending_review._GET_REVIEW_QUERY: self._get_review,
        }
        for _rel_type, query in graph_reader._ENDPOINT_QUERIES:
            self._reads[query] = self._existing_nodes(_ENDPOINT_LABELS[_rel_type])
        for _rel_type, source_label, query in graph_reader._INCOMING_EDGE_QUERIES:
            self._reads[query] = self._incoming_edges(_rel_type, source_label)
        self._flips: list[tuple[str, int, Callable[[], None]]] = []
        self._seen: dict[str, int] = {}

    def flip_before(self, marker: str, nth: int, flip: Callable[[], None]) -> None:
        """Run `flip` just before the `nth` read whose text contains `marker` (the graph moves
        between two reads of one write step, with no write path of its own).
        """
        self._flips.append((marker, nth, flip))

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        """Answer one fixed read statement from the rig graph (`timeout` is accepted, ignored)."""
        del timeout
        self.statements.append(q)
        if _WRITE_KEYWORD.search(q):
            raise AssertionError(f"the cleanup read double refuses a write statement: {q}")
        read = self._reads.get(q)
        if read is None:
            raise AssertionError(f"not a cleanup read statement the double was taught: {q}")
        for marker, nth, flip in self._flips:
            if marker in q:
                self._seen[marker] = self._seen.get(marker, 0) + 1
                if self._seen[marker] == nth:
                    flip()
        return FakeQueryResult(read(params or {}))

    # -- the reads -----------------------------------------------------------------------------

    def _capability_nodes(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [
                node_id,
                props.get("name"),
                props.get("status"),
                props.get("description"),
                props.get("type"),
                props.get("confidence"),
                props.get("created_at"),
            ]
            for (label, node_id), props in self._graph.nodes.items()
            if label == "Capability" and node_id in wanted
        ]

    def _get_review(self, params: dict[str, object]) -> list[object]:
        """One `PendingReview` as `get_pending_review` reads it (id, kind, both sides, stamps)."""
        review_id = cast("str", params["review_id"])
        review = self._graph.nodes.get(("PendingReview", review_id))
        if review is None:
            return []
        return [
            [
                review_id,
                review["kind"],
                review["incoming_id"],
                review.get("incoming_text", ""),
                review["nearest_existing_id"],
                review.get("nearest_existing_text", ""),
                review.get("similarity", 0.0),
                review.get("created_at", ""),
            ]
        ]

    def _review_merge_sides(self, params: dict[str, object]) -> list[object]:
        """The near-miss review's kind and ids, and whether each side is a live Capability."""
        review = self._graph.nodes.get(("PendingReview", cast("str", params["review_id"])))
        if review is None:
            return []
        sides = [
            self._graph.nodes.get(("Capability", cast("str", review[key])))
            for key in ("incoming_id", "nearest_existing_id")
        ]
        live = sides
        return [
            [
                review["kind"],
                review["incoming_id"],
                review["nearest_existing_id"],
                live[0] is not None,
                live[1] is not None,
                live[0].get("created_at") if live[0] else None,
                live[1].get("created_at") if live[1] else None,
            ]
        ]

    def _governed_by(self) -> list[tuple[str, str]]:
        """`(capability id, policy id)` of every `GOVERNED_BY` edge, in insertion order."""
        return [
            (key[2], key[4])
            for key in self._graph.edges
            if key[0] == "GOVERNED_BY" and key[1] == "Capability" and key[3] == "Policy"
        ]

    def _capability_governors(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        rows: list[object] = []
        for capability_id, policy_id in self._governed_by():
            policy = self._graph.nodes.get(("Policy", policy_id))
            if capability_id in wanted and policy is not None:
                rows.append([capability_id, policy_id, policy.get("title"), policy.get("status")])
        return rows

    def _policy_governed_sets(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["policy_ids"]))
        return [
            [policy_id, capability_id]
            for capability_id, policy_id in self._governed_by()
            if policy_id in wanted and ("Capability", capability_id) in self._graph.nodes
        ]

    def _governed_edge_count(self, params: dict[str, object]) -> list[object]:
        capability_id, policy_id = params["capability_id"], params["policy_id"]
        return [[sum(1 for edge in self._governed_by() if edge == (capability_id, policy_id))]]

    def _edges(self, edge_type: str, source_label: str, target_label: str) -> list[tuple[str, str]]:
        """`(source id, target id)` of every edge of the shape, one per edge, in insertion order."""
        return [
            (key[2], key[4])
            for key in self._graph.edges
            if key[0] == edge_type and key[1] == source_label and key[3] == target_label
        ]

    def _incoming_edges(
        self, edge_type: str, source_label: str
    ) -> Callable[[dict[str, object]], list[object]]:
        def read(params: dict[str, object]) -> list[object]:
            wanted = set(cast("list[str]", params["ids"]))
            return [
                [source, target]
                for source, target in self._edges(edge_type, source_label, "Capability")
                if target in wanted
            ]

        return read

    def _capability_marker(self, params: dict[str, object]) -> list[object]:
        marker = self._graph.nodes.get(("MergedCapability", cast("str", params["absorbed_id"])))
        return [[1 if marker and marker.get("merged_into") == params["survivor_id"] else 0]]

    def _capability_any_marker(self, params: dict[str, object]) -> list[object]:
        marker = self._graph.nodes.get(("MergedCapability", cast("str", params["capability_id"])))
        return [[1 if marker is not None else 0]]

    def _capability_present(self, params: dict[str, object]) -> list[object]:
        present = ("Capability", cast("str", params["capability_id"])) in self._graph.nodes
        return [[1 if present else 0]]

    def _obligation_nodes(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [node_id, props.get("text"), props.get("confidence")]
            for (label, node_id), props in self._graph.nodes.items()
            if label == "Obligation" and node_id in wanted
        ]

    def _obligation_roles(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        rows: list[object] = []
        for role_id, obligation_id in self._edges("HAS", "Role", "Obligation"):
            role = self._graph.nodes.get(("Role", role_id))
            if obligation_id in wanted and role is not None:
                rows.append([obligation_id, role_id, role.get("name")])
        return rows

    def _obligation_satisfied(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [requirement, obligation]
            for requirement, obligation in self._edges("SATISFIED_BY", "Requirement", "Obligation")
            if obligation in wanted
        ]

    def _obligation_requires(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [obligation, capability]
            for obligation, capability in self._edges("REQUIRES", "Obligation", "Capability")
            if obligation in wanted
        ]

    def _obligation_refs(self, params: dict[str, object]) -> list[object]:
        """One row per `SATISFIED_BY` Requirement, with its `EXPRESSES` `source_ref` (or None)."""
        obligation_id = params["obligation_id"]
        rows: list[object] = []
        for requirement, obligation in self._edges("SATISFIED_BY", "Requirement", "Obligation"):
            if obligation != obligation_id:
                continue
            refs = [
                props.get("source_ref")
                for key, props in self._graph.edges.items()
                if key[0] == "EXPRESSES"
                and key[1] == "RegulatoryInstrument"
                and key[3] == "Requirement"
                and key[4] == requirement
            ]
            rows.extend([requirement, ref] for ref in (refs or [None]))
        return rows

    def _obligation_marker(self, params: dict[str, object]) -> list[object]:
        marker = self._graph.nodes.get(("MergedObligation", cast("str", params["absorbed_id"])))
        return [[1 if marker and marker.get("merged_into") == params["survivor_id"] else 0]]

    def _obligation_any_marker(self, params: dict[str, object]) -> list[object]:
        marker = self._graph.nodes.get(("MergedObligation", cast("str", params["obligation_id"])))
        return [[1 if marker is not None else 0]]

    def _obligation_present(self, params: dict[str, object]) -> list[object]:
        present = ("Obligation", cast("str", params["obligation_id"])) in self._graph.nodes
        return [[1 if present else 0]]

    # -- the unmerge reads ---------------------------------------------------------------------

    def _existing_nodes(self, label: str) -> Callable[[dict[str, object]], list[object]]:
        def read(params: dict[str, object]) -> list[object]:
            wanted = set(cast("list[str]", params["ids"]))
            return [[i] for (lbl, i) in self._graph.nodes if lbl == label and i in wanted]

        return read

    def _policy_exists(self, params: dict[str, object]) -> list[object]:
        policy_id = cast("str", params["policy_id"])
        return [[policy_id]] if ("Policy", policy_id) in self._graph.nodes else []

    def _capability_markers(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [node_id, props.get("merged_into")]
            for (label, node_id), props in self._graph.nodes.items()
            if label == "MergedCapability" and node_id in wanted
        ]

    def _obligation_markers(self, params: dict[str, object]) -> list[object]:
        wanted = set(cast("list[str]", params["ids"]))
        return [
            [node_id, props.get("merged_into")]
            for (label, node_id), props in self._graph.nodes.items()
            if label == "MergedObligation" and node_id in wanted
        ]

    def _role_exists(self, params: dict[str, object]) -> list[object]:
        role_id = cast("str", params["role_id"])
        return [[role_id]] if ("Role", role_id) in self._graph.nodes else []

    def _requirements_exist(self, params: dict[str, object]) -> list[object]:
        return self._existing_nodes("Requirement")(params)

    def _capabilities_exist(self, params: dict[str, object]) -> list[object]:
        return self._existing_nodes("Capability")(params)


class CleanupGraphSeeder:
    """Seeds fixture graphs as a pre-gateway writer left them (edges without an identity)."""

    graph: InMemoryGraph

    def __init__(self, graph: InMemoryGraph) -> None:
        """Seed into `graph`."""
        self.graph = graph

    def seed_capability(
        self,
        capability_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        properties: Mapping[str, object] | None = None,
    ) -> None:
        """Seed a Capability (`status=None` leaves it unset: a legacy node counts as active)."""
        node: dict[str, object] = {"name": name or capability_id, **(properties or {})}
        if status is not None:
            node["status"] = status
        self.graph.seed_node(("Capability",), capability_id, node)

    def seed_policy(self, policy_id: str, *, title: str, status: str) -> None:
        """Seed a Policy."""
        self.graph.seed_node(("Policy",), policy_id, {"title": title, "status": status})

    def seed_edge(
        self,
        edge: tuple[str, str, str, str, str],
        *,
        legacy: bool,
        properties: Mapping[str, object] | None = None,
    ) -> None:
        """Seed an edge `(type, source label, source id, target label, target id)`."""
        if legacy:
            self.graph.seed_legacy_edge(edge, dict(properties or {}))
            return
        identity = f"{edge[0]}:{edge[2]}->{edge[4]}"
        key = (*edge, identity)
        self.graph.edges[key] = {"identity": identity, **(properties or {})}
        self.graph.edge_internal_id(key)

    def seed_governs(self, capability_id: str, policy_id: str, *, legacy: bool = False) -> None:
        """Seed a `(Capability)-[:GOVERNED_BY]->(Policy)` edge."""
        self.seed_edge(
            ("GOVERNED_BY", "Capability", capability_id, "Policy", policy_id), legacy=legacy
        )

    def seed_endpoint(self, label: str, node_id: str, **properties: object) -> None:
        """Seed an edge endpoint node (an Obligation, PracticeArea, Requirement, ...)."""
        self.graph.seed_node((label,), node_id, dict(properties))

    def seed_near_miss_review(
        self,
        review_id: str,
        *,
        incoming_id: str,
        existing_id: str,
        kind: str = "Capability",
        incoming_created_at: str | None = None,
        existing_created_at: str | None = None,
    ) -> None:
        """Seed a `PendingReview` of the pair; the `created_at` stamps go on the Capabilities."""
        for capability_id, created_at in (
            (incoming_id, incoming_created_at),
            (existing_id, existing_created_at),
        ):
            node = self.graph.nodes.get(("Capability", capability_id))
            if node is not None and created_at is not None:
                node["created_at"] = created_at
        self.seed_endpoint(
            "PendingReview",
            review_id,
            kind=kind,
            incoming_id=incoming_id,
            nearest_existing_id=existing_id,
        )

    def seed_capability_merge_world(
        self,
        *,
        governed: tuple[tuple[str, str, str], ...] = (),
        legacy: bool = False,
    ) -> None:
        """The default capability-merge fixture, the one `ScriptedMergeGraph` scripts.

        `SURVIVOR` and `ABSORBED` are active; `ABSORBED` has two `REQUIRES` sources (`obl_2` also
        requires `SURVIVOR`) and one `COVERS` source. `governed` is `(capability, policy, policy
        status)` triples: each Policy is seeded and governs the capability plus the unrelated
        `OTHER_CAPABILITY`, so a Policy's governed set is never just the pair.
        """
        self.seed_capability(
            SURVIVOR,
            name="Survivor",
            status="active",
            properties={"description": "desc", "confidence": 0.9},
        )
        self.seed_capability(
            ABSORBED,
            name="Absorbed",
            status="active",
            properties={"type": "technical", "confidence": 0.8},
        )
        self.seed_capability(OTHER_CAPABILITY, name=OTHER_CAPABILITY, status="active")
        for obligation in ("obl_1", "obl_2"):
            self.seed_endpoint("Obligation", obligation)
        self.seed_endpoint("PracticeArea", "pa_1")
        for source, target in (("obl_1", ABSORBED), ("obl_2", ABSORBED), ("obl_2", SURVIVOR)):
            self.seed_edge(("REQUIRES", "Obligation", source, "Capability", target), legacy=legacy)
        self.seed_edge(("COVERS", "PracticeArea", "pa_1", "Capability", ABSORBED), legacy=legacy)
        for capability, policy, status in governed:
            if ("Policy", policy) not in self.graph.nodes:
                self.seed_policy(policy, title="Incident Policy", status=status)
                self.seed_governs(OTHER_CAPABILITY, policy, legacy=legacy)
            self.seed_governs(capability, policy, legacy=legacy)

    def seed_obligation_merge_world(self, *, legacy: bool = False) -> None:
        """The default obligation-merge fixture, the one `ScriptedObligationGraph` scripts.

        Two Obligations under `ROLE_ID`; the absorbed one is satisfied by `req_1` and `req_2`
        (which also satisfies the survivor) and requires `cap_1` (which the survivor requires
        too). The Requirements are expressed by an instrument with `Art. 6(1)` / `Art. 6(2)`.
        """
        self.seed_endpoint("Role", ROLE_ID, name="Manufacturer")
        self.seed_endpoint("Obligation", OBL_SURVIVOR, text="Report incidents", confidence=0.9)
        self.seed_endpoint("Obligation", OBL_ABSORBED, text="Report  incidents.", confidence=0.8)
        self.seed_endpoint("RegulatoryInstrument", "reg_1")
        self.seed_endpoint("Capability", "cap_1", name="cap_1", status="active")
        for requirement in ("req_1", "req_2"):
            self.seed_endpoint("Requirement", requirement)
        for obligation in (OBL_SURVIVOR, OBL_ABSORBED):
            self.seed_edge(("HAS", "Role", ROLE_ID, "Obligation", obligation), legacy=legacy)
        for requirement, obligation in (
            ("req_1", OBL_ABSORBED),
            ("req_2", OBL_ABSORBED),
            ("req_2", OBL_SURVIVOR),
        ):
            self.seed_edge(
                ("SATISFIED_BY", "Requirement", requirement, "Obligation", obligation),
                legacy=legacy,
            )
        for obligation in (OBL_ABSORBED, OBL_SURVIVOR):
            self.seed_edge(
                ("REQUIRES", "Obligation", obligation, "Capability", "cap_1"), legacy=legacy
            )
        for requirement, source_ref in (("req_1", "Art. 6(1)"), ("req_2", "Art. 6(2)")):
            self.seed_edge(
                ("EXPRESSES", "RegulatoryInstrument", "reg_1", "Requirement", requirement),
                legacy=legacy,
                properties={"source_ref": source_ref},
            )

    def seed_capability_unmerge_world(
        self, *, policy: str | None = None, legacy: bool = False
    ) -> None:
        """The aftermath of the merge `capability_merge_history()` records.

        `ABSORBED` is deleted and a `MergedCapability` marker points at `SURVIVOR`.

        `SURVIVOR` holds the moved edges (`REQUIRES` `obl_1`/`obl_2`, `COVERS` `pa_1`,
        `MITIGATED_BY` `rp_1`; `obl_2` was already there). `policy` is the history's governance:
        `None` (case 1), `"moved"` (case 2:
        the survivor now holds the absorbed Capability's `GOVERNED_BY` edge) or `"kept"` (case 3:
        the survivor was governed by the same Policy before the merge and still is).
        """
        self.seed_capability(
            SURVIVOR,
            name="Survivor",
            status="active",
            properties={"description": "desc", "confidence": 0.9},
        )
        self.seed_endpoint("MergedCapability", ABSORBED, merged_into=SURVIVOR)
        for label, node_id in (
            ("Obligation", "obl_1"),
            ("Obligation", "obl_2"),
            ("PracticeArea", "pa_1"),
            ("RiskPath", "rp_1"),
        ):
            self.seed_endpoint(label, node_id)
        self.seed_policy(POLICY, title="Incident Policy", status="approved")
        for edge in (
            ("REQUIRES", "Obligation", "obl_1", "Capability", SURVIVOR),
            ("REQUIRES", "Obligation", "obl_2", "Capability", SURVIVOR),
            ("COVERS", "PracticeArea", "pa_1", "Capability", SURVIVOR),
            ("MITIGATED_BY", "RiskPath", "rp_1", "Capability", SURVIVOR),
        ):
            self.seed_edge(edge, legacy=legacy)
        if policy is not None:
            self.seed_governs(SURVIVOR, POLICY, legacy=legacy)

    def seed_obligation_unmerge_world(self, *, legacy: bool = False) -> None:
        """The aftermath of the merge `obligation_merge_history()` records.

        `OBL_ABSORBED` is gone and a `MergedObligation` marker points at `OBL_SURVIVOR`, which holds
        `SATISFIED_BY` `req_1`/`req_2` and `REQUIRES` `cap_1` under `ROLE_ID`.
        """
        self.seed_endpoint("Role", ROLE_ID, name="Manufacturer")
        self.seed_endpoint("Obligation", OBL_SURVIVOR, text="Report incidents", confidence=0.9)
        self.seed_endpoint("MergedObligation", OBL_ABSORBED, merged_into=OBL_SURVIVOR)
        self.seed_endpoint("Capability", "cap_1", name="cap_1", status="active")
        for requirement in ("req_1", "req_2"):
            self.seed_endpoint("Requirement", requirement)
        self.seed_edge(("HAS", "Role", ROLE_ID, "Obligation", OBL_SURVIVOR), legacy=legacy)
        for requirement in ("req_1", "req_2"):
            self.seed_edge(
                ("SATISFIED_BY", "Requirement", requirement, "Obligation", OBL_SURVIVOR),
                legacy=legacy,
            )
        self.seed_edge(
            ("REQUIRES", "Obligation", OBL_SURVIVOR, "Capability", "cap_1"), legacy=legacy
        )

    def seed_release_world(self, *, policy_status: str = "draft", legacy: bool = False) -> None:
        """The default release fixture: `SURVIVOR` and `OTHER_CAPABILITY` governed by `POLICY`.

        The same facts `ScriptedReleaseGraph` scripts: `SURVIVOR` is active with a description and
        a confidence, `POLICY` is the draft "Incident Policy" and governs one other Capability.
        """
        self.seed_capability(
            SURVIVOR,
            name="Survivor",
            status="active",
            properties={"description": "desc", "confidence": 0.9},
        )
        self.seed_capability(ABSORBED, name="Absorbed", status="active")
        self.seed_capability(OTHER_CAPABILITY, name=OTHER_CAPABILITY, status="active")
        self.seed_policy(POLICY, title="Incident Policy", status=policy_status)
        self.seed_governs(SURVIVOR, POLICY, legacy=legacy)
        self.seed_governs(OTHER_CAPABILITY, POLICY, legacy=legacy)


class HookedSubmitter:
    """The rig gateway with a one-shot hook just before the delegated `submit_group`.

    `read_log_position` is not hooked, so a callback appended through the real gateway lands
    exactly between a guarded flow's position read and its submit: the stale-position window.
    """

    def __init__(self, gateway: object) -> None:
        self._gateway = cast("_RigGateway", gateway)
        self._before_submit: Callable[[], None] | None = None
        self.calls: list[str] = []
        """Gateway calls in order (`read_log_position`, `is_caught_up`, `submit_group`, ...)."""
        self.groups: list[MutationGroup] = []
        """Every group handed to `submit_group` (the gateway submits its own stamp groups)."""

    def before_submit(self, callback: Callable[[], None]) -> None:
        """Run `callback` once, just before the next delegated `submit_group`."""
        self._before_submit = callback

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Run the pending hook (once), then submit through the real gateway."""
        self.calls.append("submit_group")
        self.groups.append(group)
        callback, self._before_submit = self._before_submit, None
        if callback is not None:
            callback()
        return self._gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        """Delegate."""
        self.calls.append("is_caught_up")
        return self._gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        """Delegate."""
        self.calls.append("stamp_legacy_edge_identity")
        return self._gateway.stamp_legacy_edge_identity(graph, audit_event_id)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        """Delegate."""
        return self._gateway.read_persisted_elements(graph)

    def read_log_position(self, graph: str) -> int:
        """Delegate (never hooked)."""
        self.calls.append("read_log_position")
        return self._gateway.read_log_position(graph)


class _RigGateway:
    """Structural stand-in for the typing of the rig's real gateway (what the hook delegates to)."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome: ...
    def is_caught_up(self, graph: str) -> bool: ...
    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome: ...
    def read_persisted_elements(self, graph: str) -> PersistedElements: ...
    def read_log_position(self, graph: str) -> int: ...


class CleanupRig(GatewayRig, CleanupGraphSeeder):
    """A gateway rig that also hands out `policy_system`, its read handle and an audit store."""

    def __init__(self) -> None:
        super().__init__()
        CleanupGraphSeeder.__init__(self, self.graphs.open(POLICY_SYSTEM))
        self.read = CleanupReadGraph(self.graph)
        self.audit = RecordingAuditStore(
            events=self.events, event_id_sink=self.store.audit_rows.add
        )
        self.hooked = HookedSubmitter(self.gateway)
        """The gateway behind a one-shot `before_submit` hook (the stale-injection seam)."""

    def dependencies(
        self,
        roles: AccessRoleStore,
        *,
        submitter: object | None = None,
    ) -> GraphCleanupDependencies:
        """The dependency bundle for this rig: its read handle, audit store and (hooked) gateway."""
        gateway = submitter if submitter is not None else self.hooked

        def _gateway(_config: ServiceConfig) -> object:
            return gateway

        return GraphCleanupDependencies(
            open_single_tenant_graph=lambda _config: self.read,
            audit_store=lambda _config: self.audit,
            access_role_store=lambda _config: roles,
            graph_write_gateway=_gateway,  # pyright: ignore[reportArgumentType] -- the rig gateway satisfies `CleanupSubmitter` structurally
        )

    def order(self) -> list[str]:
        """The audit and log-append events of this rig, in order (audit row before its group)."""
        return [e for e in self.events if e.startswith("audit:") or e == "log_append"]

    def append_unrelated_group(self) -> None:
        """Append one group of an unrelated write through the real gateway (moves the position)."""
        self.gateway.submit_group(
            MutationGroup(
                graph=POLICY_SYSTEM,
                primitives=(UpsertNode(label="Capability", id="cap_unrelated", properties={}),),
            )
        )

    @property
    def logged_groups(self) -> list[GraphLogGroup]:
        """The groups the log holds for `policy_system`, oldest first."""
        return [g for g in self.store.groups if g.graph == POLICY_SYSTEM]

    def cleanup_groups(self) -> list[GraphLogGroup]:
        """The logged groups that carry an audit link (the stamp and unrelated ones may not)."""
        return [g for g in self.logged_groups if self.store.links[g.group_id] is not None]


def caught_up_gateway(_config: ServiceConfig) -> CleanupSubmitter:
    """A `graph_write_gateway` factory for tests that only reconcile: a fresh rig, caught up."""
    return CleanupRig().gateway
