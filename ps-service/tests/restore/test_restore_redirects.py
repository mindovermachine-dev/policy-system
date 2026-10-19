"""Offline restore follows `Merged<Kind>` markers.

Re-restoring an artifact that regenerates a node a Compliance Officer cleanup merge absorbed must
not mint it again: the restore drops it, re-targets its edges onto the terminal survivor, and
upserts `HAS` onto the survivor (idempotent). The `policy_system` leg is a tuple of gateway
primitives built from reads of the live graph, so the expectations are asserted on primitives.
The live graph is a hand-written fake satisfying `GraphHandle` structurally.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    CompanyMergeValidationError,
)
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.models import BareEdge
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
    SerializedPropertyValue,
)
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore import merge_primitives
from ps_service.restore.merge_primitives import build_policy_system_leg
from restore._gateway_fakes import ScriptedLive

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import BaselineGraph

_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"
_ROLE = "role_r"
_REQUIREMENT = "REG-R-1.0_req_art_1.1"
_ABSORBED = obligation_id(_ROLE, "Do the thing.")
_SURVIVOR = obligation_id(_ROLE, "Do the thing")
_TERMINAL = obligation_id(_ROLE, "Do the thing now")
_CAP = capability_id("Thing Doing")
_ROLE_ABSORBED = "role_operator_aaaaaa"
_ROLE_SURVIVOR = "role_operator_bbbbbb"
_ROLE_TERMINAL = "role_operator_cccccc"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _capability_node(node_id: str, name: str, embedding: list[float] | None) -> SerializedNode:
    properties: dict[str, SerializedPropertyValue] = {
        "id": node_id,
        "name": name,
        "confidence": 0.8,
    }
    if embedding is not None:
        properties["embedding"] = embedding
    return SerializedNode("Capability", properties)


def _baseline(
    role_id: str = _ROLE,
    capabilities: tuple[tuple[str, str, list[float] | None], ...] = ((_CAP, "Thing Doing", None),),
) -> SerializedGraph:
    """One Role, one Requirement, the regenerated (absorbed) Obligation requiring one Capability."""

    def edge(
        kind: str,
        src: tuple[str, str],
        tgt: tuple[str, str],
        properties: dict[str, SerializedPropertyValue] | None = None,
    ) -> SerializedEdge:
        return SerializedEdge(kind, src[0], src[1], tgt[0], tgt[1], properties or {})

    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode("Role", {"id": role_id, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Requirement",
                {
                    "id": _REQUIREMENT,
                    "text": "Must do.",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": role_id,
                },
            ),
            SerializedNode(
                "Obligation", {"id": _ABSORBED, "text": "Do the thing.", "confidence": 0.9}
            ),
            *(_capability_node(*capability) for capability in capabilities),
        ),
        edges=(
            edge(
                "DEFINES",
                ("RegulatoryInstrument", _INSTRUMENT_ID),
                ("Role", role_id),
                {"source_ref": "Article 1(1)"},
            ),
            edge("HAS", ("Role", role_id), ("Obligation", _ABSORBED)),
            edge("SATISFIED_BY", ("Requirement", _REQUIREMENT), ("Obligation", _ABSORBED)),
            edge("REQUIRES", ("Obligation", _ABSORBED), ("Capability", capabilities[0][0])),
        ),
    )


def _live(
    markers: dict[str, str] | None = None, *, existing: frozenset[str] = frozenset({_SURVIVOR})
) -> ScriptedLive:
    """The live `policy_system`: the given Obligations, with `MergedObligation` markers."""
    held = markers if markers is not None else {_ABSORBED: _SURVIVOR}

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(m:MergedObligation)" in q:
            return [[absorbed, target] for absorbed, target in held.items()]
        return None

    return ScriptedLive(existing={"Obligation": set(existing)} if existing else {}, answer=answer)


def _build(
    live: ScriptedLive, baseline: SerializedGraph | None = None
) -> tuple[UpsertNode | UpsertEdge, ...]:
    leg = build_policy_system_leg(
        baseline or _baseline(),
        instrument_id=_INSTRUMENT_ID,
        live=live,
        governance=GovernanceImport(),
        similarity_threshold=_THRESHOLD,
        emitter=None,
    )
    return tuple(p for p in leg.primitives if isinstance(p, UpsertNode | UpsertEdge))


def _edges(primitives: tuple[UpsertNode | UpsertEdge, ...], edge_type: str) -> list[UpsertEdge]:
    return [p for p in primitives if isinstance(p, UpsertEdge) and p.type == edge_type]


def test_restore_never_mints_the_absorbed_obligation() -> None:
    primitives = _build(_live())

    assert [p for p in primitives if isinstance(p, UpsertNode) and p.label == "Obligation"] == []
    referenced = {
        endpoint.id
        for p in primitives
        for endpoint in ((p.source, p.target) if isinstance(p, UpsertEdge) else ())
    } | {p.id for p in primitives if isinstance(p, UpsertNode)}
    assert _ABSORBED not in referenced


def test_restore_lands_satisfied_by_and_requires_on_the_survivor() -> None:
    primitives = _build(_live())

    satisfied = _edges(primitives, "SATISFIED_BY")
    requires = _edges(primitives, "REQUIRES")
    assert [(e.source.id, e.target.id) for e in satisfied] == [(_REQUIREMENT, _SURVIVOR)]
    assert [(e.source.id, e.target.id) for e in requires] == [(_SURVIVOR, _CAP)]


def test_restore_re_merges_the_has_edge_onto_the_survivor_without_a_duplicate() -> None:
    primitives = _build(_live())

    has = _edges(primitives, "HAS")
    assert [(e.source.id, e.target.id) for e in has] == [(_ROLE, _SURVIVOR)]
    assert [e.identity for e in has] == [f"HAS:{_ROLE}->{_SURVIVOR}"]


def test_restore_follows_a_chain_to_the_terminal() -> None:
    live = _live({_ABSORBED: _SURVIVOR, _SURVIVOR: _TERMINAL}, existing=frozenset({_TERMINAL}))

    satisfied = _edges(_build(live), "SATISFIED_BY")

    assert [(e.source.id, e.target.id) for e in satisfied] == [(_REQUIREMENT, _TERMINAL)]


def _live_roles(markers: dict[str, str], existing: frozenset[str]) -> ScriptedLive:
    """The live `policy_system`: the given Roles, with `MergedRole` markers."""

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(m:MergedRole)" in q:
            return [[absorbed, target] for absorbed, target in markers.items()]
        return None

    return ScriptedLive(existing={"Role": set(existing)}, answer=answer)


def test_restore_never_mints_the_absorbed_role_and_lands_has_on_the_survivor() -> None:
    live = _live_roles({_ROLE_ABSORBED: _ROLE_SURVIVOR}, frozenset({_ROLE_SURVIVOR}))

    primitives = _build(live, _baseline(_ROLE_ABSORBED))

    assert [p.id for p in primitives if isinstance(p, UpsertNode) and p.label == "Role"] == []
    assert [(e.source.id, e.target.id) for e in _edges(primitives, "HAS")] == [
        (_ROLE_SURVIVOR, _ABSORBED)
    ]
    referenced = {
        endpoint.id
        for p in primitives
        for endpoint in ((p.source, p.target) if isinstance(p, UpsertEdge) else ())
    } | {p.id for p in primitives if isinstance(p, UpsertNode)}
    assert _ROLE_ABSORBED not in referenced


def test_restore_follows_a_role_chain_to_the_terminal() -> None:
    live = _live_roles(
        {_ROLE_ABSORBED: _ROLE_SURVIVOR, _ROLE_SURVIVOR: _ROLE_TERMINAL},
        frozenset({_ROLE_TERMINAL}),
    )

    primitives = _build(live, _baseline(_ROLE_ABSORBED))

    assert [e.source.id for e in _edges(primitives, "HAS")] == [_ROLE_TERMINAL]


def test_restore_keeps_defines_edge_to_a_live_only_terminal_role() -> None:
    """The terminal Role is in no edge but `DEFINES` and not in the artifact; live holds it."""
    baseline = SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode("Role", {"id": _ROLE_ABSORBED, "name": "Operator", "confidence": 0.9}),
        ),
        edges=(
            SerializedEdge(
                "DEFINES",
                "RegulatoryInstrument",
                _INSTRUMENT_ID,
                "Role",
                _ROLE_ABSORBED,
                {"source_ref": "Article 1(1)"},
            ),
        ),
    )
    live = _live_roles({_ROLE_ABSORBED: _ROLE_TERMINAL}, frozenset({_ROLE_TERMINAL}))

    primitives = _build(live, baseline)

    assert [(e.source.id, e.target.id) for e in _edges(primitives, "DEFINES")] == [
        (_INSTRUMENT_ID, _ROLE_TERMINAL)
    ]


def test_restore_fails_closed_when_the_role_survivor_no_longer_exists() -> None:
    live = _live_roles({_ROLE_ABSORBED: _ROLE_SURVIVOR}, frozenset())

    with pytest.raises(CompanyMergeValidationError):
        _build(live, _baseline(_ROLE_ABSORBED))


def test_restore_fails_closed_when_the_survivor_no_longer_exists() -> None:
    live = _live(existing=frozenset())

    with pytest.raises(CompanyMergeValidationError):
        _build(live)


def test_restore_fails_closed_on_a_marker_cycle() -> None:
    live = _live({_ABSORBED: _SURVIVOR, _SURVIVOR: _ABSORBED})

    with pytest.raises(CompanyMergeValidationError):
        _build(live)


def test_restore_reports_a_classification_endpoint_that_only_a_marker_holds_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id that exists only as a merge marker is not a node a classification edge may use.

    A dangling edge cannot come out of a serialized artifact, so the read is the seam.
    """
    marker_only = obligation_id(_ROLE, "Gone")
    real = merge_primitives.graph_reader.read_baseline_graph

    def read_with_dangling_edge(graph: GraphHandle, instrument_id: str) -> BaselineGraph:
        found = real(graph, instrument_id)
        stray = BareEdge(relationship_type="COVERS", source_id=marker_only, target_id=_CAP)
        return replace(found, classification_edges=(stray,))

    # detroit-exception: a dangling edge is unreachable from a real artifact; the read is the seam
    monkeypatch.setattr(
        merge_primitives.graph_reader, "read_baseline_graph", read_with_dangling_edge
    )
    live = ScriptedLive(existing={"MergedObligation": {marker_only}})

    with pytest.raises(CompanyMergePersistenceError, match=marker_only):
        _build(live)


_CAP_ABSORBED = capability_id("Thing Doing Quickly")
_CAP_SURVIVOR = capability_id("Thing Doing Promptly")
_CAP_TERMINAL = capability_id("Thing Doing Promptly Now")
_CAP_EXISTING = capability_id("Thing Doing Existing")


def _live_capabilities(
    markers: dict[str, str],
    existing: frozenset[str],
    index: tuple[tuple[str, str, list[float]], ...] = (),
) -> ScriptedLive:
    """The live `policy_system`: the given Capabilities, with `MergedCapability` markers."""

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(m:MergedCapability)" in q:
            return [[absorbed, target] for absorbed, target in markers.items()]
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return [[node_id, name, embedding] for node_id, name, embedding in index]
        return None

    return ScriptedLive(existing={"Capability": set(existing)}, answer=answer)


def _absorbed_capability_baseline() -> SerializedGraph:
    return _baseline(capabilities=((_CAP_ABSORBED, "Thing Doing Quickly", [1.0, 0.0]),))


def test_restore_lands_requires_on_the_surviving_capability_and_mints_none() -> None:
    live = _live_capabilities({_CAP_ABSORBED: _CAP_SURVIVOR}, frozenset({_CAP_SURVIVOR}))

    primitives = _build(live, _absorbed_capability_baseline())

    assert [p for p in primitives if isinstance(p, UpsertNode) and p.label == "Capability"] == []
    assert [(e.source.id, e.target.id) for e in _edges(primitives, "REQUIRES")] == [
        (_ABSORBED, _CAP_SURVIVOR)
    ]


def test_restore_follows_a_capability_chain_to_the_terminal() -> None:
    live = _live_capabilities(
        {_CAP_ABSORBED: _CAP_SURVIVOR, _CAP_SURVIVOR: _CAP_TERMINAL}, frozenset({_CAP_TERMINAL})
    )

    primitives = _build(live, _absorbed_capability_baseline())

    assert [e.target.id for e in _edges(primitives, "REQUIRES")] == [_CAP_TERMINAL]


def test_restore_fails_closed_when_the_capability_survivor_no_longer_exists() -> None:
    live = _live_capabilities({_CAP_ABSORBED: _CAP_SURVIVOR}, frozenset())

    with pytest.raises(CompanyMergeValidationError):
        _build(live, _absorbed_capability_baseline())


def test_restore_lands_on_the_dedup_canonical_id_of_a_redirected_capabilitys_terminal() -> None:
    """The survivor is in the artifact and converges onto a Capability the live graph holds."""
    baseline = _baseline(
        capabilities=(
            (_CAP_ABSORBED, "Thing Doing Quickly", [1.0, 0.0]),
            (_CAP_SURVIVOR, "Thing Doing Promptly", [1.0, 0.0]),
        )
    )
    live = _live_capabilities(
        {_CAP_ABSORBED: _CAP_SURVIVOR},
        frozenset({_CAP_EXISTING}),
        index=((_CAP_EXISTING, "Thing Doing Existing", [1.0, 0.0]),),
    )

    primitives = _build(live, baseline)

    assert [p for p in primitives if isinstance(p, UpsertNode) and p.label == "Capability"] == []
    assert [e.target.id for e in _edges(primitives, "REQUIRES")] == [_CAP_EXISTING]


def test_restore_counts_no_skipped_embedding_for_an_absorbed_capability(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """The absorbed Capability is gone from the live graph, so it cannot be a skipped candidate."""
    emitter, log_path = make_emitter()
    live = _live_capabilities(
        {_CAP_ABSORBED: _CAP_SURVIVOR},
        frozenset({_CAP_SURVIVOR}),
        index=((_CAP_SURVIVOR, "Thing Doing Promptly", [0.0, 1.0]),),
    )

    build_policy_system_leg(
        _absorbed_capability_baseline(),
        instrument_id=_INSTRUMENT_ID,
        live=live,
        governance=GovernanceImport(),
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )
    emitter.flush()

    assert [
        row
        for row in read_lines(log_path)
        if row.get("action") == "resolve_capability_convergence_offline"
    ] == []


def test_restore_never_offers_an_absorbed_capability_as_a_review_candidate() -> None:
    """A fresh Capability scoring near the survivor is reviewed against the survivor only."""
    live = _live_capabilities(
        {_CAP_ABSORBED: _CAP_SURVIVOR},
        frozenset({_CAP_SURVIVOR}),
        index=((_CAP_SURVIVOR, "Thing Doing Promptly", [0.6, 0.8]),),
    )
    baseline = _baseline(capabilities=((_CAP, "Thing Doing", [1.0, 0.0]),))

    primitives = _build(live, baseline)

    reviews = [p for p in primitives if isinstance(p, UpsertNode) and p.label == "PendingReview"]
    assert [r.properties["nearest_existing_id"] for r in reviews] == [_CAP_SURVIVOR]
