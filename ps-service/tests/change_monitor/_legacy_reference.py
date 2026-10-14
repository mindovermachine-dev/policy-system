"""Frozen copy of Change Monitor's five succession write statements as they were before the
gateway migration.

The statement texts are the Cypher of the pre-migration `change_monitor/succession.py`, verbatim.
Each is pinned by a sha256 taken from the rendered constants at commit 730f843 at freeze time
(`test_frozen_statements_match_their_pinned_hashes`), so an edit to a "verbatim" constant fails.

`LegacyRecordingGraph` is a small interpreter that keeps the node and edge sets those statements
would have produced. Only the parameters are read; the statement text selects the shape. A
statement the interpreter does not know raises, so the reference can only claim what it has been
taught. A missing node is a silent no-op, exactly as the old `MATCH` made it.

Met modulo edge identity: the legacy graph carries no edge identity, the gateway stamps
`TYPE:source->target`; `as_gateway_graph` applies the same formula before a digest comparison.
The live digest and live interpreter agreement are not verifiable here (no FalkorDB in this
sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import InMemoryGraph

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "FROZEN_STATEMENTS",
    "FROZEN_STATEMENT_HASHES",
    "LegacyRecordingGraph",
    "legacy_clear_marker",
    "legacy_link_and_supersede",
    "legacy_mark_stage",
    "legacy_set_version",
    "legacy_supersede_in_single_tenant",
]

EdgeKey = tuple[str, str, str, str, str]
"""`(type, source label, source id, target label, target id)`."""

# sha256 of each rendered statement text, taken from commit 730f843 at freeze.
FROZEN_STATEMENT_HASHES: dict[str, str] = {
    "set_version": "17e6ab356cba9db783596dc08dc9e6009ef5a830c20595dc45e64f2ea173baa5",
    "mark_stage": "6b16a8758736846bc013a4385582da7bd682ebfa1eb51e7907db0a5115cdd096",
    "link_and_supersede": "58987b5abc44f2a61ef6f1dbda337ee12451275c197de02fac4e5df8bb5bd1df",
    "supersede_in_single_tenant": (
        "79d036a8d57382475b3d5ea7f47845eab8d77d594192c18b9180615599bdeefe"
    ),
    "clear_marker": "d9e400500587a79193c0eef544987129797591f0df54457e085307cbc08041a3",
}

_SET_VERSION = "MATCH (n:RegulatoryInstrument {id: $new_id}) SET n.version = $new_version"

_MARK_STAGE = """MERGE (m:ReingestProgress {id: $new_id})
SET m.stage = $stage"""

_LINK_AND_SUPERSEDE = """MATCH (prior:RegulatoryInstrument {id: $prior_id}),
      (new:RegulatoryInstrument {id: $new_id})
MERGE (prior)-[e:SUPERSEDED_BY]->(new)
SET e.absorbed = true, prior.status = 'superseded'
WITH new
MERGE (m:ReingestProgress {id: $new_id})
SET m.stage = 'linked'"""

_SUPERSEDE_IN_SINGLE_TENANT = """MATCH (prior:RegulatoryInstrument {id: $prior_id}),
      (new:RegulatoryInstrument {id: $new_id})
MERGE (prior)-[:SUPERSEDED_BY]->(new)
SET prior.status = 'superseded'
RETURN prior.id AS prior_id"""

_CLEAR_MARKER = "MATCH (m:ReingestProgress {id: $new_id}) DELETE m"

FROZEN_STATEMENTS: dict[str, str] = {
    "set_version": _SET_VERSION,
    "mark_stage": _MARK_STAGE,
    "link_and_supersede": _LINK_AND_SUPERSEDE,
    "supersede_in_single_tenant": _SUPERSEDE_IN_SINGLE_TENANT,
    "clear_marker": _CLEAR_MARKER,
}

_RI = "RegulatoryInstrument"
_MARKER = "ReingestProgress"


@dataclass(frozen=True, slots=True)
class _Result:
    """The rows a legacy statement returned (only the single-tenant supersede returns any)."""

    result_set: list[object] = field(default_factory=list)


@dataclass
class LegacyRecordingGraph:
    """A legacy `GraphHandle`: records every write statement and the graph those calls build."""

    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[EdgeKey, dict[str, object]] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)

    @classmethod
    def copy_of(cls, graph: InMemoryGraph) -> LegacyRecordingGraph:
        """The nodes and edges of `graph` as a legacy graph (an edge's identity is not kept)."""
        copy = cls()
        copy.nodes = {key: dict(props) for key, props in graph.nodes.items()}
        copy.edges = {
            (key[0], key[1], key[2], key[3], key[4]): {
                name: value for name, value in props.items() if name != "identity"
            }
            for key, props in graph.edges.items()
        }
        return copy

    def as_gateway_graph(self) -> InMemoryGraph:
        """The graph the calls built, every edge stamped with the gateway's identity.

        The comparison is "modulo edge identity": the legacy graph carries no identity, the
        gateway stamps `TYPE:source->target` on every edge it writes.
        """
        graph = InMemoryGraph()
        for (label, node_id), properties in self.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for (
            edge_type,
            source_label,
            source_id,
            target_label,
            target_id,
        ), props in self.edges.items():
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {"identity": identity, **props}
            graph.edge_internal_id(key)
        return graph

    # -- the interpreter -----------------------------------------------------------------------

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one frozen legacy statement; any other shape is a failure of the reference."""
        assert params is not None, f"a frozen write statement carries parameters: {q}"
        handler = self._dispatch(q)
        if handler is None:
            raise AssertionError(f"not a frozen legacy statement the reference was taught: {q}")
        self.statements.append(q)
        return handler(params)

    def _dispatch(self, q: str) -> Callable[[dict[str, object]], _Result] | None:
        fixed: dict[str, Callable[[dict[str, object]], _Result]] = {
            _SET_VERSION: self._set_version,
            _MARK_STAGE: self._mark_stage,
            _LINK_AND_SUPERSEDE: self._link_and_supersede,
            _SUPERSEDE_IN_SINGLE_TENANT: self._supersede_in_single_tenant,
            _CLEAR_MARKER: self._clear_marker,
        }
        return fixed.get(q)

    def _set_version(self, params: dict[str, object]) -> _Result:
        node = self.nodes.get((_RI, cast("str", params["new_id"])))
        if node is not None:
            node["version"] = params["new_version"]
        return _Result()

    def _mark_stage(self, params: dict[str, object]) -> _Result:
        node = self.nodes.setdefault((_MARKER, cast("str", params["new_id"])), {})
        node["stage"] = params["stage"]
        return _Result()

    def _link_and_supersede(self, params: dict[str, object]) -> _Result:
        """Edge, `absorbed`, prior status and marker together; nothing if a node is missing."""
        prior_id, new_id = cast("str", params["prior_id"]), cast("str", params["new_id"])
        prior = self.nodes.get((_RI, prior_id))
        if prior is None or (_RI, new_id) not in self.nodes:
            return _Result()
        edge = self.edges.setdefault(("SUPERSEDED_BY", _RI, prior_id, _RI, new_id), {})
        edge["absorbed"] = True
        prior["status"] = "superseded"
        self.nodes.setdefault((_MARKER, new_id), {})["stage"] = "linked"
        return _Result()

    def _supersede_in_single_tenant(self, params: dict[str, object]) -> _Result:
        prior_id, new_id = cast("str", params["prior_id"]), cast("str", params["new_id"])
        prior = self.nodes.get((_RI, prior_id))
        if prior is None or (_RI, new_id) not in self.nodes:
            return _Result()
        self.edges.setdefault(("SUPERSEDED_BY", _RI, prior_id, _RI, new_id), {})
        prior["status"] = "superseded"
        return _Result([[prior_id]])

    def _clear_marker(self, params: dict[str, object]) -> _Result:
        self.nodes.pop((_MARKER, cast("str", params["new_id"])), None)
        return _Result()


# -- one runner per shape: the frozen statement with the parameters the old writer sent ----------


def legacy_set_version(graph: LegacyRecordingGraph, new_id: str, new_version: str) -> None:
    """Run the frozen version write."""
    graph.query(_SET_VERSION, {"new_id": new_id, "new_version": new_version})


def legacy_mark_stage(graph: LegacyRecordingGraph, new_id: str, stage: str) -> None:
    """Run the frozen stage-marker write."""
    graph.query(_MARK_STAGE, {"new_id": new_id, "stage": stage})


def legacy_link_and_supersede(graph: LegacyRecordingGraph, prior_id: str, new_id: str) -> None:
    """Run the frozen fused native succession write."""
    graph.query(_LINK_AND_SUPERSEDE, {"prior_id": prior_id, "new_id": new_id})


def legacy_supersede_in_single_tenant(
    graph: LegacyRecordingGraph, prior_id: str, new_id: str
) -> bool:
    """Run the frozen `policy_system` write; whether it matched (the old code raised if not)."""
    result = graph.query(_SUPERSEDE_IN_SINGLE_TENANT, {"prior_id": prior_id, "new_id": new_id})
    return bool(result.result_set)


def legacy_clear_marker(graph: LegacyRecordingGraph, new_id: str) -> None:
    """Run the frozen marker delete."""
    graph.query(_CLEAR_MARKER, {"new_id": new_id})
