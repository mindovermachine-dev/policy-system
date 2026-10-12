"""Offline restore follows `MergedObligation` markers (issue #190, H1 / CHANGES.md A7).

Re-restoring an artifact that regenerates an Obligation a Compliance Officer cleanup merge
absorbed must not mint it again: the restore drops it, re-targets its `SATISFIED_BY` / `REQUIRES`
edges onto the survivor, and upserts `HAS` onto the survivor (idempotent). Since #214 the
`policy_system` leg is a tuple of gateway primitives built from reads of the live graph, so the
expectations are asserted on primitives. The live graph is a hand-written fake satisfying
`GraphHandle` structurally.
"""

from __future__ import annotations

import pytest

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore.merge_primitives import build_policy_system_leg
from restore._gateway_fakes import ScriptedLive

_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"
_ROLE = "role_r"
_REQUIREMENT = "REG-R-1.0_req_art_1.1"
_ABSORBED = obligation_id(_ROLE, "Do the thing.")
_SURVIVOR = obligation_id(_ROLE, "Do the thing")
_CAP = capability_id("Thing Doing")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _baseline() -> SerializedGraph:
    """One Role, one Requirement, the regenerated (absorbed) Obligation requiring one Capability."""

    def edge(kind: str, src: tuple[str, str], tgt: tuple[str, str]) -> SerializedEdge:
        return SerializedEdge(kind, src[0], src[1], tgt[0], tgt[1], {})

    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode("Role", {"id": _ROLE, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Requirement",
                {
                    "id": _REQUIREMENT,
                    "text": "Must do.",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": _ROLE,
                },
            ),
            SerializedNode(
                "Obligation", {"id": _ABSORBED, "text": "Do the thing.", "confidence": 0.9}
            ),
            SerializedNode("Capability", {"id": _CAP, "name": "Thing Doing", "confidence": 0.8}),
        ),
        edges=(
            edge("HAS", ("Role", _ROLE), ("Obligation", _ABSORBED)),
            edge("SATISFIED_BY", ("Requirement", _REQUIREMENT), ("Obligation", _ABSORBED)),
            edge("REQUIRES", ("Obligation", _ABSORBED), ("Capability", _CAP)),
        ),
    )


def _live(*, survivor_exists: bool = True) -> ScriptedLive:
    """The live `policy_system`: the survivor Obligation, with a `MergedObligation` marker."""

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "MergedObligation" in q:
            return [[_ABSORBED, _SURVIVOR]]
        if "MATCH (o:Obligation) WHERE o.id IN $ids" in q:
            return [[_SURVIVOR]] if survivor_exists else []
        return None

    return ScriptedLive(
        existing={"Obligation": {_SURVIVOR}} if survivor_exists else {}, answer=answer
    )


def _build(live: ScriptedLive) -> tuple[UpsertNode | UpsertEdge, ...]:
    leg = build_policy_system_leg(
        _baseline(),
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


def test_restore_fails_closed_when_the_survivor_no_longer_exists() -> None:
    live = _live(survivor_exists=False)

    with pytest.raises(CompanyMergeValidationError):
        _build(live)
