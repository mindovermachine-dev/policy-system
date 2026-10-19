"""A `GraphHandle` fake that holds `Merged<Kind>` markers and live node ids per mergeable kind.

It answers the reads `company_merge.redirects` issues (the marker read, the batched existence
read of one kind and the live `DEFINES` read) from what a test seeded, records every statement,
and refuses a write statement: the resolver reads, it never writes. When given a gateway rig
graph it also answers the per-label existence read, the marker read and the live `DEFINES`
read from that graph, so a merge run over the rig (a real cleanup merge leaves a
`Merged<Kind>` node there) is seen by the resolver.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from graph_gateway._fakes import InMemoryGraph

_MARKER_READ = re.compile(r"^MATCH \(m:Merged(?P<kind>\w+)\) RETURN m\.id, m\.merged_into$")
_EXISTENCE_READ = re.compile(r"^MATCH \(n:(?P<kind>\w+)\) WHERE n\.id IN \$ids RETURN n\.id$")
_DEFINES_READ = (
    "MATCH (:RegulatoryInstrument {id: $instrument_id})-[:DEFINES]->(n:Role) "
    "WHERE n.id IN $ids RETURN n.id"
)
_WRITE_CLAUSE = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b")


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class MarkerGraph:
    """Scripted markers (`kind -> {absorbed id: merged_into}`), live ids (`kind -> {id}`) and the
    Roles the live graph already holds a `DEFINES` edge to (`instrument id -> {role id}`).
    """

    def __init__(
        self,
        *,
        markers: Mapping[str, Mapping[str, str | None]] | None = None,
        existing: Mapping[str, frozenset[str]] | None = None,
        defines: Mapping[str, frozenset[str]] | None = None,
        gateway_graph: InMemoryGraph | None = None,
    ) -> None:
        self._markers = markers or {}
        self._defines = defines or {}
        self._existing = existing or {}
        self._gateway_graph = gateway_graph
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        if gateway_graph is not None:
            for kind, ids in self._existing.items():
                for node_id in ids:
                    gateway_graph.seed_node((kind,), node_id, {})

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Answer one read; a write statement is a test failure."""
        self.calls.append((q, params))
        if _WRITE_CLAUSE.search(q):
            message = f"the redirect resolver wrote through its read handle: {q}"
            raise AssertionError(message)
        marker = _MARKER_READ.match(q)
        if marker is not None:
            kind = marker.group("kind")
            rows = dict(self._markers.get(kind, {}))
            if self._gateway_graph is not None:
                rows |= {
                    node_id: cast("str | None", props.get("merged_into"))
                    for (label, node_id), props in self._gateway_graph.nodes.items()
                    if label == f"Merged{kind}"
                }
            return _Result([[absorbed, target] for absorbed, target in rows.items()])
        existence = _EXISTENCE_READ.match(q)
        if existence is not None:
            kind = existence.group("kind")
            held = set(self._existing.get(kind, frozenset()))
            if self._gateway_graph is not None:
                held |= {i for label, i in self._gateway_graph.nodes if label == kind}
            return _Result([[i] for i in cast("list[str]", (params or {})["ids"]) if i in held])
        if q == _DEFINES_READ:
            return self._defines_rows(params or {})
        return _Result([])

    def _defines_rows(self, params: dict[str, object]) -> _Result:
        """The `$ids` Roles the live graph holds a `DEFINES` edge to from `$instrument_id`."""
        instrument_id = cast("str", params["instrument_id"])
        held = set(self._defines.get(instrument_id, frozenset()))
        if self._gateway_graph is not None:
            held |= {
                key[4]
                for key in self._gateway_graph.edges
                if key[0] == "DEFINES" and key[2] == instrument_id and key[3] == "Role"
            }
        return _Result([[i] for i in cast("list[str]", params["ids"]) if i in held])
