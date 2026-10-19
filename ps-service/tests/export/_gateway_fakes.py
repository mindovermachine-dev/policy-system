"""The Graph Write Gateway rig, seen from Export.

Export reads the `{short}_baseline` and `{short}_native` graphs through a query handle and writes
the baseline graph only as a gateway group (the embedding backfill). `ExportRig` is the process's
gateway rig (in-memory log and FalkorDB fakes, the true boundaries) with the two graphs of one
instrument; `ExportReadGraph` is the read handle over a rig graph.

`ExportReadGraph` answers the fixed reads `run_export`, the backfill and `serialize_graph` issue,
from the rig graph's own nodes and edges, so it sees every node a gateway group wrote. It refuses
any write statement: Export writes nothing but through the gateway.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import FakeQueryResult, GatewayRig, InMemoryGraph

from ps_service.company_merge.models import PendingReviewRecord
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.ingestion.falkordb_client import native_graph_name

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from ps_service.graph_gateway.models import GraphLogGroup

_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DETACH|DROP)\b")
_EXISTENCE = "MATCH (n:RegulatoryInstrument {id: $id}) RETURN properties(n)"
_MISSING_EMBEDDING = re.compile(
    r"MATCH \(n:(\w+)\) WHERE n\.embedding IS NULL RETURN n\.id, n\.(\w+)"
)
_NODE_IDS = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id$")
_NODE_ROWS = re.compile(r"MATCH \(n:(\w+)\) RETURN labels\(n\), properties\(n\)")
_EDGE_ROWS = re.compile(
    r"MATCH \(s\)-\[r:(\w+)\]->\(t\) RETURN labels\(s\), s\.id, labels\(t\), t\.id, properties\(r\)"
)


class ExportReadGraph:
    """A read-only `_GraphQueryHandle` over a rig graph; every write statement is refused."""

    def __init__(self, graph: InMemoryGraph, *, fail_reads_with: Exception | None = None) -> None:
        self._graph = graph
        self._fail_reads_with = fail_reads_with
        self.statements: list[str] = []
        """Every statement received, in order."""

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Answer one fixed read; refuse anything that writes."""
        self.statements.append(q)
        if _WRITE_KEYWORD.search(q):
            raise AssertionError(
                f"Export wrote a graph directly instead of through the gateway: {q}"
            )
        if self._fail_reads_with is not None:
            raise self._fail_reads_with
        if q == "RETURN 1":
            return FakeQueryResult([[1]])
        if q == _EXISTENCE:
            node = self._graph.nodes.get(("RegulatoryInstrument", str((params or {})["id"])))
            if node is None:
                return FakeQueryResult([])
            return FakeQueryResult([[{"id": str((params or {})["id"]), **node}]])
        return FakeQueryResult(self._answer(q))

    def _answer(self, q: str) -> list[object]:
        if match := _NODE_IDS.fullmatch(q):
            return [[node_id] for label, node_id in self._graph.nodes if label == match[1]]
        if q == "CALL db.labels()":
            return [[label] for label in sorted({label for label, _ in self._graph.nodes})]
        if q == "CALL db.relationshipTypes()":
            return [[edge_type] for edge_type in sorted({key[0] for key in self._graph.edges})]
        if q == "MATCH (n) RETURN count(n)":
            return [[len(self._graph.nodes)]]
        if match := _MISSING_EMBEDDING.fullmatch(q):
            label, text_property = match[1], match[2]
            return [
                [node_id, props.get(text_property)]
                for (node_label, node_id), props in self._graph.nodes.items()
                if node_label == label and "embedding" not in props
            ]
        if match := _NODE_ROWS.fullmatch(q):
            return [
                [[label], {"id": node_id, **props}]
                for (label, node_id), props in self._graph.nodes.items()
                if label == match[1]
            ]
        if match := _EDGE_ROWS.fullmatch(q):
            return [
                [
                    [key[1]],
                    key[2],
                    [key[3]],
                    key[4],
                    {k: v for k, v in props.items() if k != "identity"},
                ]
                for key, props in self._graph.edges.items()
                if key[0] == match[1]
            ]
        raise AssertionError(f"not an Export read statement the double was taught: {q}")


def _pending_review_row(record: PendingReviewRecord) -> list[object]:
    """One `PendingReviewRecord` in the column order `list_pending_reviews` reads."""
    return [
        record.id,
        record.kind,
        record.incoming_id,
        record.incoming_text,
        record.nearest_existing_id,
        record.nearest_existing_text,
        record.similarity,
        record.created_at,
    ]


class ScriptedPolicyGraph:
    """Handle for `policy_system`: answers only the `list_pending_reviews` statement.

    Fakes cannot run Cypher, so the "resolvable" predicate is the caller's to script: the handle
    returns the reviews it was given for whatever statement it receives and records every
    statement, so a test can compare them with what `list_pending_reviews` itself sends.
    """

    def __init__(
        self, reviews: Sequence[PendingReviewRecord] = (), *, fail_with: Exception | None = None
    ) -> None:
        self._reviews = tuple(reviews)
        self._fail_with = fail_with
        self.statements: list[str] = []
        """Every statement received, in order."""

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record `q`, then fail or return the scripted reviews as result rows."""
        _ = params
        self.statements.append(q)
        if self._fail_with is not None:
            raise self._fail_with
        return FakeQueryResult([_pending_review_row(r) for r in self._reviews])


def pending_review(
    review_id: str,
    *,
    incoming_id: str = "capability_incoming_a",
    nearest_existing_id: str = "capability_existing_a",
    kind: str = "Capability",
) -> PendingReviewRecord:
    """A `PendingReviewRecord` touching the given ids (defaults touch no instrument)."""
    return PendingReviewRecord(
        id=review_id,
        kind=kind,
        incoming_id=incoming_id,
        incoming_text="Report the incident to the authority.",
        nearest_existing_id=nearest_existing_id,
        nearest_existing_text="Conduct a risk assessment.",
        similarity=0.62,
        created_at="2026-01-01T00:00:00+00:00",
    )


@dataclass(frozen=True, slots=True)
class ArtifactDoc:
    """A curated artifact (`baseline.json` / `native.json`) as loaded JSON."""

    nodes: list[dict[str, object]]
    edges: list[dict[str, object]]


class ExportRig:
    """The process's gateway rig, with the baseline and native graphs of one instrument."""

    def __init__(self, rig: GatewayRig, *, short_name: str = "CRA") -> None:
        """Wrap `rig`; `short_name` names the graphs (`cra_baseline`, `cra_native` for `CRA`)."""
        self.rig = rig
        self.short_name = short_name
        self.baseline_name = baseline_graph_name(short_name)
        self.native_name = native_graph_name(short_name)
        self._baseline_reader: ExportReadGraph | None = None
        self._native_reader: ExportReadGraph | None = None

    @property
    def baseline(self) -> InMemoryGraph:
        """The baseline graph (`{short}_baseline`)."""
        return self.rig.graphs.open(self.baseline_name)

    @property
    def native(self) -> InMemoryGraph:
        """The native graph (`{short}_native`)."""
        return self.rig.graphs.open(self.native_name)

    def baseline_reader(self) -> ExportReadGraph:
        """The (one) read handle over the baseline graph; it records every statement."""
        if self._baseline_reader is None:
            self._baseline_reader = ExportReadGraph(self.baseline)
        return self._baseline_reader

    def native_reader(self) -> ExportReadGraph:
        """The (one) read handle over the native graph; it records every statement."""
        if self._native_reader is None:
            self._native_reader = ExportReadGraph(self.native)
        return self._native_reader

    # -- seeding (as the pipeline stages leave a graph) -----------------------------------------

    @staticmethod
    def seed_node(
        graph: InMemoryGraph, label: str, node_id: str, properties: Mapping[str, object]
    ) -> None:
        """Create a node with `properties` (the `id` is the key, not a stored property)."""
        graph.seed_node((label,), node_id, {k: v for k, v in properties.items() if k != "id"})

    @staticmethod
    def seed_edge(
        graph: InMemoryGraph,
        edge_type: str,
        source: tuple[str, str],
        target: tuple[str, str],
        properties: Mapping[str, object] | None = None,
    ) -> None:
        """Create an edge as a pre-gateway writer left it (no `identity`)."""
        graph.seed_legacy_edge(
            (edge_type, source[0], source[1], target[0], target[1]), dict(properties or {})
        )

    def seed_artifact(
        self,
        graph: InMemoryGraph,
        doc: ArtifactDoc,
        *,
        strip_embeddings_of: Collection[str] = (),
    ) -> None:
        """Load a curated artifact into `graph`, dropping `embedding` from the named labels."""
        for node in doc.nodes:
            props = dict(cast("dict[str, object]", node["properties"]))
            if node["label"] in strip_embeddings_of:
                props.pop("embedding", None)
            self.seed_node(graph, str(node["label"]), str(props["id"]), props)
        for edge in doc.edges:
            self.seed_edge(
                graph,
                str(edge["relationship_type"]),
                (str(edge["source_label"]), str(edge["source_id"])),
                (str(edge["target_label"]), str(edge["target_id"])),
                cast("dict[str, object]", edge["properties"]),
            )

    def new_audit_event_id(self) -> str:
        """An audit event id the rig's log accepts (a row some caller recorded first)."""
        event_id = str(uuid.uuid4())
        self.rig.store.audit_rows.add(event_id)
        return event_id

    def write_target(self) -> dict[str, object]:
        """The gateway keyword arguments of `export_instrument` / the backfill, on this rig."""
        return {
            "submitter": self.rig.gateway,
            "audit_event_id": self.new_audit_event_id(),
            "baseline_graph_name": self.baseline_name,
        }

    def embedding_of(self, label: str, node_id: str) -> object:
        """A node's `embedding`, or `None` when it has none."""
        return self.baseline.nodes.get((label, node_id), {}).get("embedding")

    def baseline_groups(self) -> list[GraphLogGroup]:
        """The groups the log holds for the baseline graph, oldest first."""
        return [g for g in self.rig.store.groups if g.graph == self.baseline_name]
