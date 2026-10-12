"""Test-local helpers for the D-1 spike (issue #214, stage 1). Promoted in slice 7.

`ReferenceStore` is the pre-#214 path: the interim snapshot writer's in-memory interpreter
(`SnapshotWriteStore`) extended with the provenance edge it records but does not apply, edge
properties, and an instrument of every read it serves (was the answer something the SAME run had
written?). `run_reference` drives the real `restore_instrument._run_baseline_merge` on it.

`run_new_path` is Option A: read `policy_system` at one log position, build the whole effect in
memory over an overlay (ids at that position plus ids of the primitives built so far), and
submit it as ONE group with `ExpectedPosition`. The builders for steps 7, 10 and 11 of the
classification do not exist in production code; they are written here (CHANGES.md F-4).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._legacy_reference import LEGACY_EDGE_ENDPOINT_LABELS, SnapshotWriteStore
from company_merge._seed_merge_graphs import SIMILARITY_THRESHOLD, BaselineGraphReader, StoreResult
from graph_gateway._fakes import InMemoryGraph

from ps_service.company_merge import graph_reader
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.governance_import import import_draft_governance
from ps_service.company_merge.graph_writer import (
    build_canonical_node_primitives,
    build_draft_governance_primitives,
    build_rewired_edge_primitives,
    build_role_and_requirement_primitives,
)
from ps_service.company_merge.models import GovernanceDraftCounts
from ps_service.company_merge.obligation_redirect import resolve_obligation_redirects
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.graph_gateway.models import ExpectedPosition, MutationGroup, UpsertNode
from restore._old_path import run_baseline_merge

if TYPE_CHECKING:
    from collections.abc import Mapping

    from company_merge._gateway_fakes import MergeRig
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.governance_import import GovernanceImport
    from ps_service.company_merge.models import BaselineGraph, BaselineNode
    from ps_service.export.models import SerializedGraph
    from ps_service.graph_gateway.models import Primitive
    from ps_service.logging import LogEmitter

_CURATED = Path(__file__).resolve().parents[3] / "curated-content"
_INSTRUMENT = "RegulatoryInstrument"
_PROVENANCE_TARGET = {"DEFINES": "Role", "EXPRESSES": "Requirement"}
_PROVENANCE = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\), "
    r"\(n:(\w+) \{id: \$target_id\}\) "
    r"MERGE \(r\)-\[e:(\w+)\]->\(n\) SET e\.source_ref = \$source_ref$"
)
_UNWIND_EXISTENCE = "UNWIND $ids AS id MATCH (n {id: id}) RETURN id"
_EDGE_LABELS = ("Role", "Requirement", "Obligation", "Capability", "Policy", "Standard", "Control")

KIND_INDEX = "capability index (step 4)"
KIND_MARKERS = "merged markers and obligation ids (step 5)"
KIND_ENDPOINTS = "classification endpoint existence (step 11)"
KIND_EDGE_MATCH = "edge endpoint match (steps 6 and 12)"
KIND_OTHER = "other read"
EXPECTED_OWN_WRITE_KINDS = frozenset({KIND_ENDPOINTS, KIND_EDGE_MATCH})
"""The only kinds of read the old path may answer from its own run's writes: existence by id."""


@dataclass(frozen=True)
class ArtifactBaseline:
    """One curated artifact's baseline leg, as the spike's two paths read it."""

    instrument_id: str
    graph: InMemoryGraph
    capability_embeddings: dict[str, tuple[float, ...]]
    policy_embeddings: dict[str, tuple[float, ...]]


def in_memory_graph(serialized: SerializedGraph) -> InMemoryGraph:
    graph = InMemoryGraph()
    for node in serialized.nodes:
        properties = dict(node.properties)
        node_id = cast("str", properties.pop("id"))
        graph.nodes[(node.label, node_id)] = cast("dict[str, object]", properties)
    for edge in serialized.edges:
        key = (
            edge.relationship_type,
            edge.source_label,
            edge.source_id,
            edge.target_label,
            edge.target_id,
            "",
        )
        graph.edges[key] = dict(edge.properties)
    return graph


def _embeddings(serialized: SerializedGraph, label: str) -> dict[str, tuple[float, ...]]:
    return {
        cast("str", node.properties["id"]): tuple(cast("list[float]", node.properties["embedding"]))
        for node in serialized.nodes
        if node.label == label and "embedding" in node.properties
    }


def artifact_baseline(instrument_id: str) -> ArtifactBaseline:
    """Load `curated-content/<id>/baseline.json` the way restore parses it."""
    blob = (_CURATED / instrument_id / "baseline.json").read_bytes()
    serialized = parse_serialized_graph_json(blob)
    return ArtifactBaseline(
        instrument_id=instrument_id,
        graph=in_memory_graph(serialized),
        capability_embeddings=_embeddings(serialized, "Capability"),
        policy_embeddings=_embeddings(serialized, "Policy"),
    )


@dataclass(frozen=True)
class RecordedRead:
    """One read (or endpoint match) of the old path and whether the same run had written it."""

    kind: str
    own_write: bool


class _Db:
    """A name -> `GraphHandle` lookup for `_run_baseline_merge`'s `select_graph` calls."""

    def __init__(self, graphs: Mapping[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


@dataclass
class ReferenceStore(SnapshotWriteStore):
    """The old path's snapshot, as an interpreter that also classifies every read it serves."""

    edge_props: dict[tuple[str, str, str], dict[str, object]] = field(default_factory=dict)
    reads: list[RecordedRead] = field(default_factory=list)
    _run_written: set[tuple[str, str]] = field(default_factory=set)

    def begin_run(self) -> None:
        """A new restore starts: what earlier restores wrote is now pre-existing state."""
        self._run_written.clear()

    def _write_node(self, mode: str, params: dict[str, object], label: str) -> None:
        if (label, cast("str", params["id"])) not in self.nodes:
            self._run_written.add((label, cast("str", params["id"])))
        super()._write_node(mode, params, label)

    def query(self, q: str, params: dict[str, object] | None = None) -> StoreResult:
        """Serve one statement; record the reads, apply the provenance edge the parent skips."""
        provenance = _PROVENANCE.match(q)
        if provenance is not None and params is not None:
            self.statements.append(q)
            self.params_seen.append(dict(params))
            self._write_provenance(provenance.group(1), provenance.group(2), params)
            return StoreResult([])
        edge_write = self._EDGE_WRITE.match(q)
        if edge_write is not None and params is not None:
            self._note_edge_match(edge_write.group(1), edge_write.group(2), params)
        result = super().query(q, params)
        if self._NODE_WRITE.match(q) is None and edge_write is None:
            self._note_read(q, cast("list[list[object]]", result.result_set))
        return result

    def _write_provenance(
        self, target_label: str, edge_type: str, params: dict[str, object]
    ) -> None:
        source = ("RegulatoryInstrument", cast("str", params["regulatory_instrument_id"]))
        target = (target_label, cast("str", params["target_id"]))
        if source in self.nodes and target in self.nodes:
            self.reads.append(
                RecordedRead(
                    KIND_EDGE_MATCH,
                    own_write=source in self._run_written or target in self._run_written,
                )
            )
            triple = (edge_type, source[1], target[1])
            self.edges.add(triple)
            self.edge_props[triple] = {"source_ref": params["source_ref"]}

    def _note_edge_match(
        self, source_label: str, target_label: str, params: dict[str, object]
    ) -> None:
        source = (source_label, cast("str", params["source_id"]))
        target = (target_label, cast("str", params["target_id"]))
        if source in self.nodes and target in self.nodes:
            own = source in self._run_written or target in self._run_written
            self.reads.append(RecordedRead(KIND_EDGE_MATCH, own_write=own))

    def _note_read(self, q: str, rows: list[list[object]]) -> None:
        ids = {str(row[0]) for row in rows if row}
        own_ids = {node_id for _label, node_id in self._run_written}
        own = bool(ids & own_ids)
        if q == _UNWIND_EXISTENCE:
            kind = KIND_ENDPOINTS
        elif self._INDEX_READ.match(q):
            kind = KIND_INDEX
        elif "MERGED_INTO" in q or "MergedObligation" in q or "o.id IN $ids" in q:
            kind = KIND_MARKERS
        else:
            kind, own = KIND_OTHER, False
        self.reads.append(RecordedRead(kind, own_write=own))

    def as_gateway_graph(self) -> InMemoryGraph:
        """The graph the old path built, every edge stamped with the gateway's identity."""
        graph = InMemoryGraph()
        for (label, node_id), properties in self.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for edge_type, source_id, target_id in self.edges:
            if edge_type in _PROVENANCE_TARGET:
                source_label, target_label = _INSTRUMENT, _PROVENANCE_TARGET[edge_type]
            else:
                source_label, target_label = LEGACY_EDGE_ENDPOINT_LABELS[edge_type]
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {
                "identity": identity,
                **self.edge_props.get((edge_type, source_id, target_id), {}),
            }
            graph.edge_internal_id(key)
        return graph


def masked_copy(graph: InMemoryGraph) -> InMemoryGraph:
    """A copy with every `created_at` checked to be an ISO instant and replaced by a constant.

    The old writer stamps `datetime.now` (not injectable); the new path draws from a stepped
    source. Presence and shape are checked, the instant itself is not compared.
    """
    copy = InMemoryGraph()
    for (label, node_id), properties in graph.nodes.items():
        masked = dict(properties)
        if "created_at" in masked:
            datetime.fromisoformat(str(masked["created_at"]))
            masked["created_at"] = "masked"
        copy.seed_node((label,), node_id, masked)
    for key, properties in graph.edges.items():
        copy.edges[key] = dict(properties)
        copy.edge_internal_id(key)
    return copy


def run_reference(
    store: ReferenceStore,
    baseline: ArtifactBaseline,
    emitter: LogEmitter,
    governance: GovernanceImport,
) -> dict[str, int]:
    """One restore's baseline merge on the old path: the real `_run_baseline_merge`."""
    store.begin_run()
    db = cast(
        "FalkorDB",
        _Db({"staged": BaselineGraphReader(baseline.graph), "snapshot": store}),
    )
    return run_baseline_merge(
        db,
        "staged",
        baseline.instrument_id,
        baseline.capability_embeddings,
        SIMILARITY_THRESHOLD,
        "snapshot",
        emitter,
        governance,
    )


def _ids_by_label(graph: InMemoryGraph) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = defaultdict(set)
    for label, node_id in graph.nodes:
        ids[label].add(node_id)
    return ids


def _create_only(
    label: str, nodes: tuple[BaselineNode, ...], existing: set[str]
) -> tuple[UpsertNode, ...]:
    """Test-local builder (steps 7 and 10): first occurrence of an id the graph lacks."""
    created: dict[str, UpsertNode] = {}
    for node in nodes:
        if node.id not in existing and node.id not in created:
            created[node.id] = UpsertNode(label=label, id=node.id, properties=dict(node.properties))
    return tuple(created.values())


class _Effect:
    """The primitives built so far, and the ids they create (the overlay)."""

    def __init__(self, at_position: dict[str, set[str]]) -> None:
        self.at_position = at_position
        self.created: dict[str, set[str]] = defaultdict(set)
        self.primitives: list[Primitive] = []

    def add(self, primitives: tuple[Primitive, ...]) -> None:
        for primitive in primitives:
            if isinstance(primitive, UpsertNode):
                self.created[primitive.label].add(primitive.id)
        self.primitives.extend(primitives)

    def known(self, label: str) -> set[str]:
        return self.at_position.get(label, set()) | self.created.get(label, set())

    def every_known_id(self) -> set[str]:
        known: set[str] = set()
        for label in set(self.at_position) | set(self.created):
            known |= self.known(label)
        return known


def _check_classification_endpoints(
    effect: _Effect, read: BaselineGraph, canonical: dict[str, str]
) -> None:
    """Test-local step 11: an endpoint missing from the canonical map must exist (overlay)."""
    unresolved = {
        endpoint
        for edge in read.classification_edges
        for endpoint in (edge.source_id, edge.target_id)
        if endpoint not in canonical
    }
    missing = unresolved - effect.every_known_id()
    if missing:
        message = f"classification edge endpoint(s) {sorted(missing)!r} not found"
        raise CompanyMergePersistenceError(message)


def build_effect(
    rig: MergeRig,
    read: BaselineGraph,
    governance: GovernanceImport,
    emitter: LogEmitter,
    values: ValueSource,
    incoming_embeddings: dict[str, tuple[float, ...]],
) -> tuple[Primitive, ...]:
    """The whole policy_system effect of one restore, from reads made against the live graph."""
    live: GraphHandle = rig.legacy
    dedup = resolve_capability_convergence_offline(
        read.capability_nodes,
        incoming_embeddings=incoming_embeddings,
        single_tenant_graph=live,
        threshold=SIMILARITY_THRESHOLD,
        emitter=emitter,
    )
    assert dedup.embedding_backfills == {}  # step 13: the offline backfill is always empty
    read, redirects = resolve_obligation_redirects(live, read, emitter=emitter)
    effect = _Effect(_ids_by_label(rig.policy_system))
    existing = effect.at_position
    effect.add(
        build_role_and_requirement_primitives(
            existing.get("Role", set()),
            existing.get("Requirement", set()),
            read.regulatory_instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
    )
    effect.add(_create_only("Obligation", read.obligation_nodes, existing.get("Obligation", set())))
    effect.add(
        build_canonical_node_primitives(
            existing.get("Capability", set()),
            read.capability_nodes,
            dedup.resolutions,
            kind="Capability",
            values=values,
        )
    )
    canonical = {r.incoming_id: r.canonical_id for r in dedup.resolutions}
    canonical.update(redirects)
    created_at = values.now().isoformat()

    def collect_drafts(
        _graph: GraphHandle,
        policies: tuple[BaselineNode, ...],
        standards: tuple[BaselineNode, ...],
        controls: tuple[BaselineNode, ...],
        /,
        *,
        owner: tuple[str, str],
        policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    ) -> GovernanceDraftCounts:
        nodes, overridden = build_draft_governance_primitives(
            existing, policies, standards, controls,
            owner=owner, policy_embeddings=policy_embeddings, created_at=created_at,
        )  # fmt: skip
        effect.add(nodes)
        return GovernanceDraftCounts(len(policies), len(standards), len(controls), overridden)

    import_draft_governance(
        read,
        single_tenant_graph=live,
        governance=governance,
        canonical_id_by_incoming_id=canonical,
        write_drafts=collect_drafts,
    )
    effect.add(
        _create_only("PracticeArea", read.practice_area_nodes, existing.get("PracticeArea", set()))
    )
    effect.add(_create_only("RiskPath", read.risk_path_nodes, existing.get("RiskPath", set())))
    _check_classification_endpoints(effect, read, canonical)
    effect.add(
        build_rewired_edge_primitives(
            {label: effect.known(label) for label in (*_EDGE_LABELS, "PracticeArea", "RiskPath")},
            read.bare_edges + read.governance_edges + read.classification_edges,
            canonical,
        )
    )
    return tuple(effect.primitives)


def run_new_path(
    rig: MergeRig,
    baseline: ArtifactBaseline,
    emitter: LogEmitter,
    governance: GovernanceImport,
    *,
    position_override: int | None = None,
) -> int:
    """Option A: read at position P, build in memory, submit ONE group; returns its size."""
    position = rig.gateway.read_log_position(POLICY_SYSTEM)
    read = graph_reader.read_baseline_graph(
        BaselineGraphReader(baseline.graph), baseline.instrument_id
    )
    primitives = build_effect(
        rig, read, governance, emitter, stepped_values(), baseline.capability_embeddings
    )
    expected = position if position_override is None else position_override
    rig.gateway.submit_group(
        MutationGroup(
            graph=POLICY_SYSTEM,
            audit_event_id=rig.audit_event_id,
            primitives=primitives,
            preconditions=(ExpectedPosition(position=expected),),
        )
    )
    return len(primitives)


__all__ = [
    "EXPECTED_OWN_WRITE_KINDS",
    "KIND_INDEX",
    "KIND_MARKERS",
    "ArtifactBaseline",
    "ReferenceStore",
    "artifact_baseline",
    "in_memory_graph",
    "masked_copy",
    "run_new_path",
    "run_reference",
]
