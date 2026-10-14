# pyright: reportPrivateUsage=false
"""The Graph Write Gateway rig, seen from Regulatory Change Monitor's succession.

The succession reads the native and `policy_system` graphs through a handle and writes them only
as gateway groups. `SuccessionRig` is the process's gateway rig (in-memory log and FalkorDB fakes,
the true boundaries) plus a seeding and inspection API; `SuccessionReadGraph` is the read handle
over a rig graph.

`SuccessionReadGraph` answers the four fixed succession reads from the rig graph's own nodes and
edges, so it sees every node a gateway group wrote. It refuses any write statement. During the
migration it accepts, and merely records, the frozen legacy statements named in `allowed_legacy`
(the shapes not migrated yet): they change no rig state. The allowed set shrinks per slice and is
empty once every writer is on the gateway.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import FakeQueryResult, GatewayRig, InMemoryGraph

from change_monitor._legacy_reference import FROZEN_STATEMENTS
from ps_service.change_monitor import succession
from ps_service.change_monitor.falkordb_client import native_graph_name
from ps_service.change_monitor.models import PipelineRunResult, StageSummary

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from api._audit_fakes import InMemoryAuditStore

    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GraphLogGroup, GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter

POLICY_SYSTEM = "policy_system"
_RI = "RegulatoryInstrument"
_MARKER = "ReingestProgress"
_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DETACH|DROP)\b")
_LEGACY_NAME_BY_TEXT = {text: name for name, text in FROZEN_STATEMENTS.items()}


class SuccessionReadGraph:
    """A read-only `GraphHandle` over a rig graph: the succession's fixed reads, no writes."""

    def __init__(self, graph: InMemoryGraph, *, allowed_legacy: Collection[str] = ()) -> None:
        self._graph = graph
        self._allowed_legacy = frozenset(allowed_legacy)
        self.statements: list[str] = []
        """Every statement received, in order."""
        self.legacy_statements: list[str] = []
        """The allowed frozen legacy write statements received (they change nothing)."""

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> FakeQueryResult:
        """Answer one fixed read; record (and ignore) an allowed legacy write; refuse the rest."""
        del timeout
        self.statements.append(q)
        p = params or {}
        legacy = _LEGACY_NAME_BY_TEXT.get(q)
        if legacy is not None:
            if legacy not in self._allowed_legacy:
                raise AssertionError(
                    f"the succession wrote {legacy!r} directly; it must go through the gateway"
                )
            self.legacy_statements.append(legacy)
            return FakeQueryResult(
                [[p["prior_id"]]] if legacy == "supersede_in_single_tenant" else []
            )
        if _WRITE_KEYWORD.search(q):
            raise AssertionError(f"the succession read double refuses a write statement: {q}")
        if q == succession._FIND_PRIOR_QUERY:
            return FakeQueryResult(self._find_prior(str(p["new_id"])))
        if q == succession._NEW_NODE_EXISTS_QUERY:
            node = self._graph.nodes.get((_RI, str(p["new_id"])))
            return FakeQueryResult([] if node is None else [[node.get("status")]])
        if q == succession._SUCCESSION_COMPLETE_QUERY:
            return FakeQueryResult(self._succession_complete(str(p["new_id"])))
        if q == succession._REINGESTION_FACTS_QUERY:
            return FakeQueryResult(self._facts(str(p["new_id"])))
        raise AssertionError(f"not a succession read statement the double was taught: {q}")

    def _incoming(self, new_id: str) -> list[tuple[str, dict[str, object]]]:
        return [
            (key[2], props)
            for key, props in self._graph.edges.items()
            if key[0] == "SUPERSEDED_BY" and key[1] == _RI and key[3] == _RI and key[4] == new_id
        ]

    def _find_prior(self, new_id: str) -> list[object]:
        superseded_into_new = {prior_id for prior_id, _ in self._incoming(new_id)}
        return [
            [node_id, props.get("instrument_type")]
            for (label, node_id), props in self._graph.nodes.items()
            if label == _RI
            and props.get("status") == "active"
            and node_id != new_id
            and node_id not in superseded_into_new
        ]

    def _succession_complete(self, new_id: str) -> list[object]:
        return [
            [prior_id]
            for prior_id, _ in self._incoming(new_id)
            if self._graph.nodes.get((_RI, prior_id), {}).get("status") == "superseded"
        ]

    def _facts(self, new_id: str) -> list[object]:
        if (_RI, new_id) not in self._graph.nodes:
            return []
        stage = self._graph.nodes.get((_MARKER, new_id), {}).get("stage")
        incoming = self._incoming(new_id)
        if not incoming:
            return [[None, None, None, None, stage]]
        rows: list[object] = []
        for prior_id, props in incoming:
            prior = self._graph.nodes[(_RI, prior_id)]
            rows.append(
                [
                    prior_id,
                    prior.get("instrument_type"),
                    prior.get("status"),
                    props.get("absorbed"),
                    stage,
                ]
            )
        return rows


class RecordingSubmitter:
    """The rig gateway behind a recorder of the groups submitted (what the writer asked for).

    The log holds only what the gateway applied after dropping no-ops; this holds the groups as
    submitted, primitive for primitive.
    """

    def __init__(self, gateway: GroupSubmitter) -> None:
        self._gateway = gateway
        self.groups: list[MutationGroup] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Record `group`, then submit it."""
        self.groups.append(group)
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

    def ops(self, graph: str) -> list[list[str]]:
        """Per submitted group on `graph`: the `op` of each primitive, in order."""
        return [[p.op for p in g.primitives] for g in self.groups if g.graph == graph]


class SuccessionRig:
    """The process's gateway rig, with the native and `policy_system` graphs of one instrument."""

    def __init__(self, rig: GatewayRig, *, short_name: str = "CRA") -> None:
        """Wrap `rig`; `short_name` names the native graph (`cra_native` for `CRA`)."""
        self.rig = rig
        self.short_name = short_name
        self.native_name = native_graph_name(short_name)

    @property
    def native(self) -> InMemoryGraph:
        """The native graph (`{short}_native`)."""
        return self.rig.graphs.open(self.native_name)

    @property
    def single_tenant(self) -> InMemoryGraph:
        """The merged `policy_system` graph."""
        return self.rig.graphs.open(POLICY_SYSTEM)

    def read_native(self, *, allowed_legacy: Collection[str] = ()) -> SuccessionReadGraph:
        """A read handle over the native graph."""
        return SuccessionReadGraph(self.native, allowed_legacy=allowed_legacy)

    def read_single_tenant(self, *, allowed_legacy: Collection[str] = ()) -> SuccessionReadGraph:
        """A read handle over the `policy_system` graph."""
        return SuccessionReadGraph(self.single_tenant, allowed_legacy=allowed_legacy)

    # -- seeding (as a pre-gateway writer or the pipeline stages leave the graph) -------------

    @staticmethod
    def seed_instrument(
        graph: InMemoryGraph,
        node_id: str,
        *,
        status: str = "active",
        instrument_type: str = "regulation",
        version: str = "1.0",
    ) -> None:
        """Create a `RegulatoryInstrument` node."""
        graph.seed_node(
            (_RI,),
            node_id,
            {"status": status, "instrument_type": instrument_type, "version": version},
        )

    @staticmethod
    def seed_edge(
        graph: InMemoryGraph,
        prior_id: str,
        new_id: str,
        *,
        absorbed: bool | None,
        legacy: bool = True,
    ) -> None:
        """Create a `SUPERSEDED_BY` edge; `absorbed=None` leaves the property unset.

        `legacy` leaves the edge without an `identity`, as every pre-gateway writer did.
        """
        props: dict[str, object] = {} if absorbed is None else {"absorbed": absorbed}
        edge = ("SUPERSEDED_BY", _RI, prior_id, _RI, new_id)
        if legacy:
            graph.seed_legacy_edge(edge, props)
            return
        identity = f"SUPERSEDED_BY:{prior_id}->{new_id}"
        graph.edges[(*edge, identity)] = {"identity": identity, **props}
        graph.edge_internal_id((*edge, identity))

    @staticmethod
    def seed_marker(graph: InMemoryGraph, new_id: str, stage: str) -> None:
        """Create a `ReingestProgress` marker node."""
        graph.seed_node((_MARKER,), new_id, {"stage": stage})

    # -- inspection ----------------------------------------------------------------------------

    @staticmethod
    def marker_stage(graph: InMemoryGraph, new_id: str) -> object:
        """The marker's `stage`, or `None` when there is no marker."""
        return graph.nodes.get((_MARKER, new_id), {}).get("stage")

    @staticmethod
    def status_of(graph: InMemoryGraph, node_id: str) -> object:
        """A `RegulatoryInstrument`'s `status`, or `None` when absent."""
        return graph.nodes.get((_RI, node_id), {}).get("status")

    @staticmethod
    def version_of(graph: InMemoryGraph, node_id: str) -> object:
        """A `RegulatoryInstrument`'s `version`, or `None` when absent."""
        return graph.nodes.get((_RI, node_id), {}).get("version")

    def edge_properties(
        self, graph: InMemoryGraph, prior_id: str, new_id: str
    ) -> list[dict[str, object]]:
        """The properties of every `SUPERSEDED_BY` edge from `prior_id` to `new_id`."""
        return [
            dict(props)
            for key, props in graph.edges.items()
            if key[0] == "SUPERSEDED_BY" and key[2] == prior_id and key[4] == new_id
        ]

    def groups(self, graph_name: str) -> list[GraphLogGroup]:
        """The groups the log holds for `graph_name`, oldest first."""
        return [g for g in self.rig.store.groups if g.graph == graph_name]

    def ops(self, graph_name: str) -> list[list[tuple[str, str, str]]]:
        """Per logged group of `graph_name`: `(op, name, identity)` of each entry, in order."""
        return [
            [(cast("str", e.content["op"]), e.name, e.identity) for e in group.entries]
            for group in self.groups(graph_name)
        ]

    def audit_links(self, graph_name: str) -> list[str | None]:
        """The audit event id each logged group of `graph_name` was appended with."""
        return [self.rig.store.links[g.group_id] for g in self.groups(graph_name)]

    def timeline(self) -> list[str]:
        """Stage calls and logged groups (`group:<graph>:<first entry>`), in order.

        The stage doubles append `stage:<name>` to the rig's ordered events; the log store appends
        `log_append` for every committed group.
        """
        groups = iter(self.rig.store.groups)
        timeline: list[str] = []
        for event in self.rig.events:
            if event.startswith("stage:"):
                timeline.append(event)
            elif event == "log_append":
                group = next(groups)
                timeline.append(f"group:{group.graph}:{group.entries[0].name}")
        return timeline

    def audit_store(self) -> InMemoryAuditStore:
        """An audit store whose rows' event ids the rig's log accepts as a group's anchor."""
        from api._audit_fakes import (
            InMemoryAuditStore,
        )

        return InMemoryAuditStore(event_id_sink=self.rig.store.audit_rows.add)

    def new_audit_event_id(self) -> str:
        """An audit event id the rig's log accepts (a row some caller recorded first)."""
        event_id = str(uuid.uuid4())
        self.rig.store.audit_rows.add(event_id)
        return event_id


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """A frozen copy of a graph's nodes `(label, id)` and edges `(type, source, target)`."""

    nodes: dict[tuple[str, str], dict[str, object]]
    edges: dict[tuple[str, str, str], dict[str, object]]


class RigKeyedGraph:
    """A rig graph written the way a pipeline stage writes it: MERGE-by-key, no gateway group.

    The stage doubles stand in for Ingestion, Domain Mapper and Company Merge, which are separate
    components with their own gateway tests; what they leave in the graph is seeded here directly,
    so the log holds only the succession's own groups. `edges` is the `(type, source, target)`
    view of the graph's edges without their gateway `identity`.
    """

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph

    @property
    def nodes(self) -> dict[tuple[str, str], dict[str, object]]:
        """The graph's nodes, `(label, id)` -> properties (the live dict)."""
        return self._graph.nodes

    @property
    def edges(self) -> dict[tuple[str, str, str], dict[str, object]]:
        """The graph's edges, `(type, source id, target id)` -> properties without `identity`."""
        return {
            (key[0], key[2], key[4]): {n: v for n, v in props.items() if n != "identity"}
            for key, props in self._graph.edges.items()
        }

    def merge_node(
        self, label: str, node_id: str, props: dict[str, object], *, on_create_only: bool = False
    ) -> bool:
        """`MERGE (n:label {id}) SET n += props` (or `ON CREATE SET`); `True` when created."""
        key = (label, node_id)
        created = key not in self._graph.nodes
        if created:
            self._graph.seed_node((label,), node_id, props)
        elif not on_create_only:
            self._graph.nodes[key].update(props)
        return created

    def _label_of(self, node_id: str) -> str:
        labels = [label for label, found in self._graph.nodes if found == node_id]
        assert len(labels) == 1, f"{node_id!r} must be exactly one node, found {labels}"
        return labels[0]

    def merge_edge(self, edge_type: str, src: str, dst: str) -> bool:
        """`MERGE (src)-[:edge_type]->(dst)`, identified as the gateway would; `True` if created."""
        edge = (edge_type, self._label_of(src), src, self._label_of(dst), dst)
        identity = f"{edge_type}:{src}->{dst}"
        key = (*edge, identity)
        created = key not in self._graph.edges
        if created:
            self._graph.edges[key] = {"identity": identity}
            self._graph.edge_internal_id(key)
        return created

    def count_nodes(self, label: str) -> int:
        """Number of nodes carrying `label`."""
        return sum(1 for node_label, _ in self._graph.nodes if node_label == label)

    def snapshot(self) -> GraphSnapshot:
        """A deep copy of the current content."""
        return GraphSnapshot(
            nodes={key: dict(props) for key, props in self._graph.nodes.items()},
            edges={key: dict(props) for key, props in self.edges.items()},
        )

    def status_of(self, node_id: str) -> object:
        """A `RegulatoryInstrument`'s `status`, or `None` when absent."""
        return self._graph.nodes.get((_RI, node_id), {}).get("status")

    def marker_stage(self, new_id: str) -> object:
        """The marker's `stage`, or `None` when there is no marker."""
        return self._graph.nodes.get((_MARKER, new_id), {}).get("stage")


class RigRunner:
    """A hand-written `PipelineRunner` double over a `SuccessionRig`.

    Running `stages` reports each to `on_stage_complete` after it "returns" (appending
    `stage:<name>` to the rig's ordered events first), and a completed `ingestion` creates the new
    `RegulatoryInstrument` node, as the real Ingestion stage's register write does. `fail_at`
    makes the named stage raise `RuntimeError` instead of returning, after the stages before it
    completed. The injection seam, not the code under test.
    """

    def __init__(self, rig: SuccessionRig, new_id: str, *, fail_at: str | None = None) -> None:
        self._rig = rig
        self._new_id = new_id
        self._fail_at = fail_at
        self.calls: list[tuple[str, ...]] = []
        """The `stages` of each invocation."""
        self.run_ids: list[str] = []

    def __call__(
        self, *, stages: tuple[str, ...], run_id: str, on_stage_complete: Callable[[str], None]
    ) -> PipelineRunResult:
        """Record the call, run `stages` against the rig and report each completion."""
        self.calls.append(stages)
        self.run_ids.append(run_id)
        done: list[StageSummary] = []
        for stage in stages:
            if stage == self._fail_at:
                raise RuntimeError(f"{stage} blew up")
            self._rig.rig.events.append(f"stage:{stage}")
            if stage == "ingestion" and (_RI, self._new_id) not in self._rig.native.nodes:
                self._rig.seed_instrument(self._rig.native, self._new_id)
            done.append(StageSummary(stage, {}))
            on_stage_complete(stage)
        return PipelineRunResult(stages=tuple(done))
