"""The Graph Write Gateway rig, seen from Company Merge (issue #210).

Company Merge submits its graph writes as gateway groups and only reads the graphs through a
handle. `MergeRig` is the gateway rig (in-memory log and FalkorDB fakes, the true boundaries) plus
the `policy_system` graph and the keywords the orchestrators hand `merge_baseline_graph`.

`RigReadHandle` is the read handle the merge reads `policy_system` through: it answers the
existence, embedding-state and index reads from the rig's own graph, so it sees every node the
gateway groups wrote, and it refuses a write statement (there is no legacy write path left).
"""

from __future__ import annotations

import itertools
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, cast

from graph_gateway._fakes import GatewayRig, InMemoryGraph

from company_merge._seed_merge_graphs import SingleTenantStore
from ps_service.company_merge.generated_values import ValueSource

if TYPE_CHECKING:
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.models import (
        GraphLogEntry,
        GraphLogGroup,
        GroupOutcome,
        MutationGroup,
    )
    from ps_service.ingestion.graph_submission import GroupSubmitter

POLICY_SYSTEM = "policy_system"
_FIND_REVIEW_READ = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"
_EXISTENCE_READ = re.compile(
    r"^MATCH \(r:PendingReview \{id: \$review_id\}\) "
    r"OPTIONAL MATCH \(a:(?P<kind>\w+) \{id: r\.incoming_id\}\) "
    r"WHERE coalesce\(a\.status, 'active'\) <> 'merged' "
    r"OPTIONAL MATCH \(b:(?P=kind) \{id: r\.nearest_existing_id\}\) "
    r"WHERE coalesce\(b\.status, 'active'\) <> 'merged' "
    r"RETURN r\.incoming_id AS incoming_id, r\.nearest_existing_id AS nearest_existing_id, "
    r"a IS NOT NULL AS incoming_exists, b IS NOT NULL AS existing_exists, "
    r"a\.created_at AS incoming_created_at, b\.created_at AS existing_created_at$"
)
_LOSER_EDGE_READ = re.compile(
    r"^MATCH (?:"
    r"\(n:(?P<in_other>\w+)\)-\[:(?P<in_type>\w+)\]->\(l:(?P<in_kind>\w+) \{id: \$loser_id\}\)"
    r"|\(l:(?P<out_kind>\w+) \{id: \$loser_id\}\)-\[:(?P<out_type>\w+)\]->\(n:(?P<out_other>\w+)\)"
    r") RETURN DISTINCT n\.id$"
)
BASELINE = "cra_baseline"


def stepped_values(start: datetime | None = None) -> ValueSource:
    """A deterministic `ValueSource`: each `now()` is one second after the last, ids count up.

    Two runs given a fresh source each draw the same sequence, which is how the equivalence tests
    hand the frozen reference and the gateway path identical generated values.
    """
    first = start or datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    instants = itertools.count()
    ids = itertools.count(1)
    return ValueSource(
        now=lambda: first + timedelta(seconds=next(instants)),
        new_id=lambda: f"fixed-id-{next(ids)}",
    )


class MergeGatewayKeywords(TypedDict):
    """The keywords that hand `merge_baseline_graph` the gateway and the graphs to write."""

    submitter: GroupSubmitter
    single_tenant_graph_name: str
    baseline_graph_name: str
    audit_event_id: str


class RigReadHandle(SingleTenantStore):
    """A read-only `SingleTenantStore` whose nodes and edges ARE the rig's `policy_system` graph."""

    def __init__(self, graph: InMemoryGraph) -> None:
        super().__init__(nodes=graph.nodes)
        self._graph = graph

    @property  # pyright: ignore[reportIncompatibleVariableOverride] -- a view, not a stored set
    def edges(self) -> set[tuple[str, str, str]]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """The distinct (type, source id, target id) triples the rig's graph holds (a view)."""
        return {(key[0], key[2], key[4]) for key in self._graph.edges}

    @edges.setter
    def edges(self, value: set[tuple[str, str, str]]) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        del value  # the dataclass constructor assigns an empty set; the graph is the state

    def edge_count(self, relationship: str) -> int:
        """How many `relationship` edges the rig's graph holds."""
        return sum(1 for key in self._graph.edges if key[0] == relationship)

    def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
        """The base reads, plus the resolve reads (the review's kind, from the rig's graph)."""
        if q == _FIND_REVIEW_READ:
            review = self._graph.nodes.get(
                ("PendingReview", cast("str", (params or {})["review_id"]))
            )
            return [] if review is None else [[review["kind"]]]
        existence = _EXISTENCE_READ.match(q)
        if existence is not None:
            return self._existence_row(
                existence.group("kind"), cast("str", (params or {})["review_id"])
            )
        loser_edges = _LOSER_EDGE_READ.match(q)
        if loser_edges is not None:
            return self._loser_edge_rows(loser_edges, cast("str", (params or {})["loser_id"]))
        return super()._read(q, params)

    def _existence_row(self, kind: str, review_id: str) -> list[object]:
        """The combined existence read: both nodes (not `merged`) and their `created_at`."""
        review = self._graph.nodes.get(("PendingReview", review_id))
        if review is None:
            return []

        def _node(node_id: object) -> dict[str, object] | None:
            node = self._graph.nodes.get((kind, cast("str", node_id)))
            return node if node is not None and node.get("status", "active") != "merged" else None

        incoming, existing = _node(review["incoming_id"]), _node(review["nearest_existing_id"])
        return [
            [
                review["incoming_id"],
                review["nearest_existing_id"],
                incoming is not None,
                existing is not None,
                None if incoming is None else incoming.get("created_at"),
                None if existing is None else existing.get("created_at"),
            ]
        ]

    def _loser_edge_rows(self, match: re.Match[str], loser_id: str) -> list[object]:
        """`RETURN DISTINCT n.id` of the other end of the loser's edges of one type."""
        groups = match.groupdict()
        if groups["in_type"] is not None:  # (n:Other)-[:T]->(l:Kind {id: loser})
            edge_type, other, kind = groups["in_type"], groups["in_other"], groups["in_kind"]
            ends = [
                (k[1], k[2])
                for k in self._graph.edges
                if k[0] == edge_type and (k[3], k[4]) == (kind, loser_id)
            ]
        else:  # (l:Kind {id: loser})-[:T]->(n:Other)
            edge_type, other, kind = groups["out_type"], groups["out_other"], groups["out_kind"]
            ends = [
                (k[3], k[4])
                for k in self._graph.edges
                if k[0] == edge_type and (k[1], k[2]) == (kind, loser_id)
            ]
        return [[node_id] for node_id in sorted({i for label, i in ends if label == other})]


class MergeRig(GatewayRig):
    """A gateway rig that also hands out `policy_system`, its read handle and an audit id."""

    def __init__(self, baseline_graph_name: str = BASELINE) -> None:
        super().__init__()
        self.baseline_graph_name = baseline_graph_name
        self.audit_event_id = str(uuid.uuid4())
        self.store.audit_rows.add(self.audit_event_id)
        self.policy_system: InMemoryGraph = self.graphs.open(POLICY_SYSTEM)
        """The rig's single-tenant graph: what the gateway (and the legacy mirror) has applied."""
        self.legacy = RigReadHandle(self.policy_system)

    def merge_keywords(self) -> MergeGatewayKeywords:
        """The gateway keywords the orchestrator hands the merge stage."""
        return {
            "submitter": self.gateway,
            "single_tenant_graph_name": POLICY_SYSTEM,
            "baseline_graph_name": self.baseline_graph_name,
            "audit_event_id": self.audit_event_id,
        }

    @property
    def groups(self) -> list[GraphLogGroup]:
        """Every group logged on `policy_system`, oldest first."""
        return [g for g in self.store.groups if g.graph == POLICY_SYSTEM]

    def logged(self) -> list[GraphLogEntry]:
        """Every entry logged on `policy_system`, in position order."""
        return list(self.store.entries.get(POLICY_SYSTEM, []))


@dataclass(frozen=True)
class FamilyABaseline:
    """One regulation's `{short}_baseline` graph, family A only."""

    instrument_id: str
    instrument_properties: dict[str, object]
    roles: dict[str, str] = field(default_factory=dict)  # id -> name
    requirements: dict[str, str] = field(default_factory=dict)  # id -> text
    defines: tuple[tuple[str, str], ...] = ()  # (role id, source_ref)
    expresses: tuple[tuple[str, str], ...] = ()  # (requirement id, source_ref)
    obligations: dict[str, tuple[str, float]] = field(default_factory=dict)  # id -> (text, conf.)
    capabilities: dict[str, tuple[str, float]] = field(default_factory=dict)  # id -> (name, conf.)
    practice_areas: dict[str, dict[str, str | float]] = field(default_factory=dict)  # id -> props
    risk_paths: dict[str, dict[str, str | float]] = field(default_factory=dict)  # id -> props
    edges: tuple[tuple[str, str, str, str, str], ...] = ()
    """Bare edges (type, source label, source id, target label, target id): no properties."""


def family_a_baseline_graph(baseline: FamilyABaseline) -> InMemoryGraph:
    graph = InMemoryGraph()
    graph.nodes[("RegulatoryInstrument", baseline.instrument_id)] = dict(
        baseline.instrument_properties
    )
    for role_id, name in baseline.roles.items():
        graph.nodes[("Role", role_id)] = {"name": name, "confidence": 0.9}
    for requirement_id, text in baseline.requirements.items():
        graph.nodes[("Requirement", requirement_id)] = {
            "text": text,
            "type": "requirement",
            "confidence": 0.8,
        }
    for obligation_id, (text, confidence) in baseline.obligations.items():
        graph.nodes[("Obligation", obligation_id)] = {"text": text, "confidence": confidence}
    for capability_id, (name, confidence) in baseline.capabilities.items():
        graph.nodes[("Capability", capability_id)] = {"name": name, "confidence": confidence}
    for practice_area_id, properties in baseline.practice_areas.items():
        graph.nodes[("PracticeArea", practice_area_id)] = dict(properties)
    for risk_path_id, properties in baseline.risk_paths.items():
        graph.nodes[("RiskPath", risk_path_id)] = dict(properties)
    for edge_type, source_label, source_id, target_label, target_id in baseline.edges:
        graph.edges[(edge_type, source_label, source_id, target_label, target_id, "")] = {}
    for role_id, source_ref in baseline.defines:
        key = ("DEFINES", "RegulatoryInstrument", baseline.instrument_id, "Role", role_id, "")
        graph.edges[key] = {"source_ref": source_ref}
    for requirement_id, source_ref in baseline.expresses:
        key = (
            "EXPRESSES",
            "RegulatoryInstrument",
            baseline.instrument_id,
            "Requirement",
            requirement_id,
            "",
        )
        graph.edges[key] = {"source_ref": source_ref}
    return graph


class UnusedSubmitter:
    """A `GroupSubmitter` for a call that must not write: any use of it fails the test."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        raise AssertionError(f"a gateway group was submitted where none was expected: {group}")

    def is_caught_up(self, graph: str) -> bool:
        raise AssertionError(f"the gateway was asked about {graph!r} where no write was expected")

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str
    ) -> EdgeIdentityBackfillOutcome:
        raise AssertionError(f"edges of {graph!r} were stamped where no write was expected")
