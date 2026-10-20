"""A cleanup Role merge survives live ingest, offline restore and export.

The real `execute_role_merge` runs over the gateway rig and leaves what a deployment holds: the
absorbed Role deleted, a `MergedRole {merged_into}` marker, the moved Obligations borne by the
survivor. That persisted graph (the rig graph, not a hand-built marker) is served as the
single-tenant graph to the real Company Merge (`merge_baseline_graph`), to the real offline
restore leg, and to the real `serialize_graph`, so the shared resolver
(`company_merge.redirects`) is exercised with real executor output.

Two kinds of test live here:

- `*_honours_merged_role_*` are characterisation tests of the shared redirect resolver. Each carries
  a mutation check: the same run with the marker not seeded mints the absorbed Role, so the test
  cannot pass without the marker.
- `*_keeps_the_survivors_defines_source_ref*` are the regression tests. A retargeted
  `DEFINES` edge shares the survivor's own edge identity, so the writer's last-wins `source_ref`
  upsert used to overwrite the survivor's citation with the absorbed Role's.

No FalkorDB runs here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

from company_merge._gateway_fakes import BASELINE, POLICY_SYSTEM
from company_merge._marker_graph import MarkerGraph
from company_merge._seed_merge_graphs import OneHotEmbeddingCaller
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD

from graph_cleanup._fakes import (
    ABSORBED_SOURCE_REF,
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    SURVIVOR_SOURCE_REF,
)
from graph_cleanup._merge_runs import run_role_merge
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.restore.merge_primitives import PolicySystemLeg, build_policy_system_leg

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.company_merge.falkordb_client import GraphHandle

_THRESHOLD = 0.85
_OBLIGATIONS = (OBL_OF_SURVIVOR, OBL_MOVED_1, OBL_MOVED_2)
_ROLE_NAMES = {
    ROLE_SURVIVOR: "Market Surveillance Authority",
    ROLE_ABSORBED: "market surveillance authorities",
}
_HAS = (
    (ROLE_SURVIVOR, OBL_OF_SURVIVOR),
    (ROLE_ABSORBED, OBL_MOVED_1),
    (ROLE_ABSORBED, OBL_MOVED_2),
)


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _RegulatoryInstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _RegeneratedBaseline:
    """The baseline graph the Domain Mapper writes for the instrument, read by query text.

    It regenerates every Role in `roles` (so the absorbed one too), the three Obligations and
    their `HAS` edges, and a `DEFINES` edge per Role carrying that Role's `source_ref`.
    """

    def __init__(self, roles: tuple[str, ...]) -> None:
        self._roles = roles

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:HAS]" in q:
            return _Result([[r, o] for r, o in _HAS if r in self._roles])
        if "(n:Obligation) RETURN" in q:
            return _Result([[o, f"text of {o}", 0.9] for o in _OBLIGATIONS])
        if "n.description" in q:  # the Capability read: this instrument has no Capability
            return _Result([])
        if "n.name, n.confidence" in q:
            return _Result([[r, _ROLE_NAMES[r], 0.9, None] for r in self._roles])
        if "[e:DEFINES]" in q:
            return _Result([[r, _source_ref(r)] for r in self._roles])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_RegulatoryInstrumentNode({"id": INSTRUMENT_ID, "title": "CRA"})]])
        return _Result([])


def _source_ref(role_id: str) -> str:
    return SURVIVOR_SOURCE_REF if role_id == ROLE_SURVIVOR else ABSORBED_SOURCE_REF


def _merged_world(rig: CleanupRig, *, keep_marker: bool = True) -> None:
    """Run a real Role merge; with `keep_marker=False` drop the marker (the mutation check)."""
    rig.seed_role_merge_world()
    outcome = run_role_merge(rig)
    assert outcome["merged"] is True
    assert len(rig.cleanup_groups()) == 1
    if not keep_marker:
        del rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]


def _live(rig: CleanupRig) -> MarkerGraph:
    """The single-tenant graph as the resolver and the restore see it: the rig's persisted graph."""
    return MarkerGraph(gateway_graph=rig.graph)


def _count(graph: InMemoryGraph, label: str) -> int:
    return sum(1 for node_label, _ in graph.nodes if node_label == label)


def _edges(graph: InMemoryGraph, edge_type: str) -> list[tuple[str, str, dict[str, object]]]:
    """`(source id, target id, properties without the gateway's `identity`)` per edge."""
    return [
        (k[2], k[4], {n: v for n, v in props.items() if n != "identity"})
        for k, props in graph.edges.items()
        if k[0] == edge_type
    ]


def _inbound_has(graph: InMemoryGraph, obligation_id: str) -> list[str]:
    return [source for source, target, _ in _edges(graph, "HAS") if target == obligation_id]


def _ingest(rig: CleanupRig, *, roles: tuple[str, ...]) -> None:
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    merge_baseline_graph(
        INSTRUMENT_ID,
        baseline_graph=cast("GraphHandle", _RegeneratedBaseline(roles)),
        single_tenant_graph=cast("GraphHandle", _live(rig)),
        submitter=rig.gateway,
        single_tenant_graph_name=POLICY_SYSTEM,
        baseline_graph_name=BASELINE,
        audit_event_id=audit_event_id,
        embed_model="fake-embed-model",
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
    )


def test_the_real_role_merge_leaves_what_the_resolver_reads(cleanup_rig: CleanupRig) -> None:
    _merged_world(cleanup_rig)

    assert cleanup_rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    assert ("Role", ROLE_ABSORBED) not in cleanup_rig.graph.nodes
    assert sorted(_inbound_has(cleanup_rig.graph, OBL_MOVED_1)) == [ROLE_SURVIVOR]


def test_live_ingest_honours_merged_role_and_leaves_the_graph_as_merged(
    cleanup_rig: CleanupRig,
) -> None:
    """The regenerated absorbed Role is not minted; counts and edges stay."""
    _merged_world(cleanup_rig)
    graph = cleanup_rig.graph
    roles_before, obligations_before = _count(graph, "Role"), _count(graph, "Obligation")

    _ingest(cleanup_rig, roles=(ROLE_SURVIVOR, ROLE_ABSORBED))

    assert ("Role", ROLE_ABSORBED) not in graph.nodes
    assert (_count(graph, "Role"), _count(graph, "Obligation")) == (
        roles_before,
        obligations_before,
    )
    for obligation_id in _OBLIGATIONS:
        assert _inbound_has(graph, obligation_id) == [ROLE_SURVIVOR]
    assert not any(
        ROLE_ABSORBED in (s, t) for e in ("HAS", "DEFINES") for s, t, _ in _edges(graph, e)
    )


def test_live_ingest_without_the_marker_mints_the_absorbed_role(cleanup_rig: CleanupRig) -> None:
    """Mutation check: the characterisation above fails without the `MergedRole` marker."""
    _merged_world(cleanup_rig, keep_marker=False)

    _ingest(cleanup_rig, roles=(ROLE_SURVIVOR, ROLE_ABSORBED))

    assert ("Role", ROLE_ABSORBED) in cleanup_rig.graph.nodes


def test_live_ingest_keeps_the_survivors_defines_source_ref_when_both_roles_regenerate(
    cleanup_rig: CleanupRig,
) -> None:
    """F1: the absorbed Role's `DEFINES` is not re-targeted over the survivor's own edge."""
    _merged_world(cleanup_rig)

    _ingest(cleanup_rig, roles=(ROLE_SURVIVOR, ROLE_ABSORBED))

    assert _edges(cleanup_rig.graph, "DEFINES") == [
        (INSTRUMENT_ID, ROLE_SURVIVOR, {"source_ref": SURVIVOR_SOURCE_REF})
    ]


def test_live_ingest_keeps_the_survivors_defines_source_ref_when_only_the_absorbed_role_regenerates(
    cleanup_rig: CleanupRig,
) -> None:
    """F1: the survivor is only live; its own `DEFINES` citation survives the ingest."""
    _merged_world(cleanup_rig)

    _ingest(cleanup_rig, roles=(ROLE_ABSORBED,))

    assert _edges(cleanup_rig.graph, "DEFINES") == [
        (INSTRUMENT_ID, ROLE_SURVIVOR, {"source_ref": SURVIVOR_SOURCE_REF})
    ]
    assert ("Role", ROLE_ABSORBED) not in cleanup_rig.graph.nodes
    assert _inbound_has(cleanup_rig.graph, OBL_MOVED_1) == [ROLE_SURVIVOR]


def _artifact(roles: tuple[str, ...]) -> SerializedGraph:
    """The pre-merge export artifact: every Role, Obligation, `HAS` and `DEFINES` it held."""
    nodes = [SerializedNode("RegulatoryInstrument", {"id": INSTRUMENT_ID, "title": "CRA"})]
    nodes += [
        SerializedNode("Role", {"id": r, "name": _ROLE_NAMES[r], "confidence": 0.9}) for r in roles
    ]
    nodes += [
        SerializedNode("Obligation", {"id": o, "text": f"text of {o}", "confidence": 0.9})
        for o in _OBLIGATIONS
    ]
    edges = [SerializedEdge("HAS", "Role", r, "Obligation", o, {}) for r, o in _HAS if r in roles]
    edges += [
        SerializedEdge(
            "DEFINES",
            "RegulatoryInstrument",
            INSTRUMENT_ID,
            "Role",
            r,
            {"source_ref": _source_ref(r)},
        )
        for r in roles
    ]
    return SerializedGraph(nodes=tuple(nodes), edges=tuple(edges))


def _restore(rig: CleanupRig, roles: tuple[str, ...]) -> PolicySystemLeg:
    return build_policy_system_leg(
        _artifact(roles),
        instrument_id=INSTRUMENT_ID,
        live=cast("GraphHandle", _live(rig)),
        governance=GovernanceImport(),
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=None,
    )


def _upserted_edges(leg: PolicySystemLeg, edge_type: str) -> list[UpsertEdge]:
    return [p for p in leg.primitives if isinstance(p, UpsertEdge) and p.type == edge_type]


def _role_ids(leg: PolicySystemLeg) -> list[str]:
    return [p.id for p in leg.primitives if isinstance(p, UpsertNode) and p.label == "Role"]


def test_restore_honours_merged_role_and_re_targets_has_onto_the_survivor(
    cleanup_rig: CleanupRig,
) -> None:
    """Restoring the pre-merge artifact never mints the absorbed Role."""
    _merged_world(cleanup_rig)

    leg = _restore(cleanup_rig, (ROLE_SURVIVOR, ROLE_ABSORBED))

    assert ROLE_ABSORBED not in _role_ids(leg)
    has = sorted((e.source.id, e.target.id) for e in _upserted_edges(leg, "HAS"))
    assert has == sorted((ROLE_SURVIVOR, o) for o in _OBLIGATIONS)
    assert not any(
        ROLE_ABSORBED in (p.source.id, p.target.id)
        for p in leg.primitives
        if isinstance(p, UpsertEdge)
    )


def test_restore_without_the_marker_mints_the_absorbed_role(cleanup_rig: CleanupRig) -> None:
    """Mutation check for the restore characterisation."""
    _merged_world(cleanup_rig, keep_marker=False)

    leg = _restore(cleanup_rig, (ROLE_SURVIVOR, ROLE_ABSORBED))

    assert ROLE_ABSORBED in _role_ids(leg)


def test_restore_keeps_the_survivors_defines_source_ref_when_both_roles_are_in_the_artifact(
    cleanup_rig: CleanupRig,
) -> None:
    """F1: one `DEFINES` to the survivor, with the survivor's own `source_ref`."""
    _merged_world(cleanup_rig)

    defines = _upserted_edges(_restore(cleanup_rig, (ROLE_SURVIVOR, ROLE_ABSORBED)), "DEFINES")

    assert [(e.target.id, e.properties) for e in defines] == [
        (ROLE_SURVIVOR, {"source_ref": SURVIVOR_SOURCE_REF})
    ]


def test_restore_writes_no_defines_over_a_live_only_survivor(cleanup_rig: CleanupRig) -> None:
    """F1: the artifact holds only the absorbed Role; the survivor's citation is not rewritten."""
    _merged_world(cleanup_rig)

    leg = _restore(cleanup_rig, (ROLE_ABSORBED,))

    assert _upserted_edges(leg, "DEFINES") == []
    assert ROLE_ABSORBED not in _role_ids(leg)
    assert sorted((e.source.id, e.target.id) for e in _upserted_edges(leg, "HAS")) == sorted(
        (ROLE_SURVIVOR, o) for o in (OBL_MOVED_1, OBL_MOVED_2)
    )


class _ExportView:
    """The read surface `serialize_graph` uses, answered from the rig's persisted graph."""

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        graph = self._graph
        if q == "CALL db.labels()":
            return _Result([[label] for label in sorted({label for label, _ in graph.nodes})])
        if q == "CALL db.relationshipTypes()":
            return _Result([[t] for t in sorted({k[0] for k in graph.edges})])
        if q == "MATCH (n) RETURN count(n)":
            return _Result([[len(graph.nodes)]])
        for label in {label for label, _ in graph.nodes}:
            if q == f"MATCH (n:{label}) RETURN labels(n), properties(n)":
                return _Result(
                    [
                        [[node_label], {"id": node_id, **props}]
                        for (node_label, node_id), props in graph.nodes.items()
                        if node_label == label
                    ]
                )
        for edge_type in {k[0] for k in graph.edges}:
            if f"[r:{edge_type}]" in q:
                return _Result(
                    [
                        [[k[1]], k[2], [k[3]], k[4], props]
                        for k, props in graph.edges.items()
                        if k[0] == edge_type
                    ]
                )
        message = f"unexpected export query: {q!r}"
        raise AssertionError(message)


def test_export_after_a_role_merge_carries_no_marker_and_no_absorbed_role(
    cleanup_rig: CleanupRig,
) -> None:
    """The artifact holds the survivor, its Obligations and its own `DEFINES`."""
    _merged_world(cleanup_rig)
    assert ("MergedRole", ROLE_ABSORBED) in cleanup_rig.graph.nodes  # the marker is in the graph

    exported = serialize_graph(cast("GraphHandle", _ExportView(cleanup_rig.graph)))  # pyright: ignore[reportArgumentType]

    assert {n.label for n in exported.nodes} == {"RegulatoryInstrument", "Role", "Obligation"}
    assert [n.properties["id"] for n in exported.nodes if n.label == "Role"] == [ROLE_SURVIVOR]
    assert not any(ROLE_ABSORBED in (e.source_id, e.target_id) for e in exported.edges)
    assert [
        (e.target_id, e.properties["source_ref"])
        for e in exported.edges
        if e.relationship_type == "DEFINES"
    ] == [(ROLE_SURVIVOR, SURVIVOR_SOURCE_REF)]
    assert sorted(e.target_id for e in exported.edges if e.relationship_type == "HAS") == sorted(
        _OBLIGATIONS
    )
