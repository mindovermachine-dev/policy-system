"""A read-only `GraphHandle` over a parsed artifact's baseline leg.

`company_merge.graph_reader.read_baseline_graph` is the single source of the baseline
projections; this handle answers its fixed queries (`graph_reader.BASELINE_QUERIES`) from the
parsed `SerializedGraph`, so Restore reads its own input without holding a FalkorDB handle.

The answers are those a graph written from the artifact would give: the artifact folded as a
written graph would hold it (`leg_primitives.fold_serialized_graph`), an absent property read as
`NULL`, a query filtered by the instrument id it is parameterised with, and an edge read only
between nodes of the labels the query names. Any other statement is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ps_service.company_merge.graph_reader import BASELINE_QUERIES
from ps_service.restore.leg_primitives import fold_serialized_graph

if TYPE_CHECKING:
    from ps_service.export.models import SerializedGraph

__all__ = ["SerializedGraphReader", "UnansweredBaselineQueryError"]

_NODE_COLUMNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "role": ("Role", ("id", "name", "confidence")),
    "requirement": ("Requirement", ("id", "text", "type", "confidence", "role_id")),
    "obligation": ("Obligation", ("id", "text", "confidence")),
    "capability": ("Capability", ("id", "name", "confidence", "description", "embedding")),
    "practice_area": (
        "PracticeArea",
        ("id", "name", "status", "description", "version", "owner_id"),
    ),
    "risk_path": ("RiskPath", ("id", "name", "status", "description", "risk_type", "version")),
}
_WHOLE_NODES = {
    "regulatory_instrument": "RegulatoryInstrument",
    "policy": "Policy",
    "standard": "Standard",
    "control": "Control",
}
_PROVENANCE = {"defines": ("DEFINES", "Role"), "expresses": ("EXPRESSES", "Requirement")}
_BARE_EDGES = {
    "has": ("HAS", "Role", "Obligation"),
    "satisfied_by": ("SATISFIED_BY", "Requirement", "Obligation"),
    "requires": ("REQUIRES", "Obligation", "Capability"),
    "governed_by": ("GOVERNED_BY", "Capability", "Policy"),
    "supported_by": ("SUPPORTED_BY", "Policy", "Standard"),
    "implemented_by": ("IMPLEMENTED_BY", "Standard", "Control"),
    "covers": ("COVERS", "PracticeArea", "Capability"),
    "owns": ("OWNS", "PracticeArea", "Policy"),
    "mitigated_by": ("MITIGATED_BY", "RiskPath", "Capability"),
    "verified_by": ("VERIFIED_BY", "RiskPath", "Control"),
}
_NAME_BY_QUERY = {query: name for name, query in BASELINE_QUERIES.items()}


class UnansweredBaselineQueryError(Exception):
    """The reader was asked a statement that is not one of the baseline reader's fixed queries.

    Raised by `SerializedGraphReader.query`; a caller bug (it passed a statement the baseline
    reader never issues), not a data condition, so there is nothing to retry.
    """


@dataclass(frozen=True)
class _Rows:
    result_set: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class _NodeView:
    """The `.properties` view of a `falkordb.Node` that `RETURN n` yields."""

    properties: dict[str, object]


class SerializedGraphReader:
    """Answers `graph_reader.BASELINE_QUERIES` from a parsed artifact; never writes.

    Stands in for a FalkorDB `GraphHandle` so `read_baseline_graph` can read an artifact's
    baseline leg without the graph being staged anywhere. Holds only in-memory indexes of the
    artifact; no I/O.
    """

    def __init__(self, graph: SerializedGraph) -> None:
        """Index the artifact's folded nodes by label and edges by (type, labels).

        Args:
            graph: The parsed baseline leg; folded the way `leg_primitives.fold_serialized_graph`
                folds it for writing, so reads see what a written graph would hold.
        """
        folded = fold_serialized_graph(graph)
        self._nodes: dict[str, list[dict[str, object]]] = {}
        for node in folded.nodes:
            self._nodes.setdefault(node.label, []).append(dict(node.properties))
        self._edges: dict[tuple[str, str, str], list[tuple[str, str, dict[str, object]]]] = {}
        for edge in folded.edges:
            key = (edge.relationship_type, edge.source_label, edge.target_label)
            self._edges.setdefault(key, []).append(
                (edge.source_id, edge.target_id, dict(edge.properties))
            )

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        """Answer one of the reader's fixed queries from the artifact.

        An absent property reads as `None`. The regulatory-instrument node and the provenance
        edges are filtered by the `regulatory_instrument_id` parameter; every other query
        returns all rows of its labels.

        Args:
            q: Exact text of one of `graph_reader.BASELINE_QUERIES`.
            params: Query parameters; only `regulatory_instrument_id` is read, and an absent
                key matches no instrument.

        Returns:
            The result rows in the shape the real graph returns for that query; empty when the
            artifact holds no matching nodes or edges.

        Raises:
            UnansweredBaselineQueryError: `q` is not a registered baseline query.
        """
        name = _NAME_BY_QUERY.get(q)
        if name is None:
            raise UnansweredBaselineQueryError(f"not a baseline reader query: {q!r}")
        instrument_id = (params or {}).get("regulatory_instrument_id")
        if name in _NODE_COLUMNS:
            label, columns = _NODE_COLUMNS[name]
            return _Rows(
                [[node.get(column) for column in columns] for node in self._nodes.get(label, [])]
            )
        if name in _WHOLE_NODES:
            nodes = self._nodes.get(_WHOLE_NODES[name], [])
            if name == "regulatory_instrument":
                nodes = [node for node in nodes if node.get("id") == instrument_id]
            return _Rows([[_NodeView(node)] for node in nodes])
        if name in _PROVENANCE:
            edge_type, target_label = _PROVENANCE[name]
            edges = self._edges.get((edge_type, "RegulatoryInstrument", target_label), [])
            return _Rows(
                [
                    [target, properties.get("source_ref")]
                    for source, target, properties in edges
                    if source == instrument_id
                ]
            )
        edge_type, source_label, target_label = _BARE_EDGES[name]
        edges = self._edges.get((edge_type, source_label, target_label), [])
        return _Rows([[source, target] for source, target, _properties in edges])
