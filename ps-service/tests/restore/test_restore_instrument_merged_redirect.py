"""Offline restore follows `MERGED_INTO` tombstone redirects (issue #190, AC-BI-008/009).

A restored baseline artifact regenerates a Capability whose id is a `merged` tombstone in the
target deployment. The restore resolves it through `MERGED_INTO` to the survivor: the incoming
`REQUIRES` edge attaches to the survivor and no Capability node is minted. Since #214 the
`policy_system` leg is a tuple of gateway primitives built from reads of the live graph, so the
expectations are asserted on primitives. The live graph is a hand-written fake satisfying
`GraphHandle` structurally, mirroring the sibling restore tests.
"""

from __future__ import annotations

import pytest

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore.merge_primitives import build_policy_system_leg
from restore._gateway_fakes import ScriptedLive

_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _baseline(
    *, capability_id_value: str, obligation_id_value: str, embedding: list[float]
) -> SerializedGraph:
    """One Obligation requiring one Capability (which carries the artifact's embedding)."""
    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": _INSTRUMENT_ID, "title": "R"}),
            SerializedNode(
                "Obligation",
                {"id": obligation_id_value, "text": "Do the thing.", "confidence": 0.9},
            ),
            SerializedNode(
                "Capability",
                {
                    "id": capability_id_value,
                    "name": "Absorbed",
                    "confidence": 0.8,
                    "embedding": embedding,
                },
            ),
        ),
        edges=(
            SerializedEdge(
                "REQUIRES",
                "Obligation",
                obligation_id_value,
                "Capability",
                capability_id_value,
                {},
            ),
        ),
    )


def _live(*, absorbed_id: str, survivor_id: str) -> ScriptedLive:
    """The live `policy_system`: two Capabilities, one of them a tombstone of the other."""

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "MergedObligation" in q:
            return []
        if "MERGED_INTO" in q:
            return [[absorbed_id, survivor_id]]
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return [
                [absorbed_id, "Absorbed", [1.0, 0.0]],
                [survivor_id, "Survivor", [0.0, 1.0]],
            ]
        return None

    return ScriptedLive(existing={"Capability": {absorbed_id, survivor_id}}, answer=answer)


def _build(baseline: SerializedGraph, live: ScriptedLive) -> tuple[UpsertNode | UpsertEdge, ...]:
    leg = build_policy_system_leg(
        baseline,
        instrument_id=_INSTRUMENT_ID,
        live=live,
        governance=GovernanceImport(),
        similarity_threshold=_THRESHOLD,
        emitter=None,
    )
    return tuple(p for p in leg.primitives if isinstance(p, UpsertNode | UpsertEdge))


def _requires(primitives: tuple[UpsertNode | UpsertEdge, ...]) -> list[tuple[str, str]]:
    return [
        (p.source.id, p.target.id)
        for p in primitives
        if isinstance(p, UpsertEdge) and p.type == "REQUIRES"
    ]


def _capabilities(primitives: tuple[UpsertNode | UpsertEdge, ...]) -> list[UpsertNode]:
    return [p for p in primitives if isinstance(p, UpsertNode) and p.label == "Capability"]


def test_restore_attaches_requires_to_survivor_and_mints_no_capability() -> None:
    absorbed_id = capability_id("Absorbed")
    survivor_id = capability_id("Survivor")
    obl_id = obligation_id("role_r", "Do the thing.")
    baseline = _baseline(
        capability_id_value=absorbed_id, obligation_id_value=obl_id, embedding=[1.0, 0.0]
    )

    primitives = _build(baseline, _live(absorbed_id=absorbed_id, survivor_id=survivor_id))

    assert _capabilities(primitives) == []
    assert _requires(primitives) == [(obl_id, survivor_id)]


def test_restore_never_scores_a_tombstone_as_a_semantic_candidate() -> None:
    """AC-BI-009 offline half: a tombstone's cached embedding is not in the candidate pool."""
    tomb_id = capability_id("Absorbed")
    survivor_id = capability_id("Survivor")
    new_id = capability_id("Brand New")
    obl_id = obligation_id("role_r", "Do the thing.")
    # Identical to the tombstone's vector: would semantic-merge onto it if it were a candidate.
    baseline = _baseline(
        capability_id_value=new_id, obligation_id_value=obl_id, embedding=[1.0, 0.0]
    )

    primitives = _build(baseline, _live(absorbed_id=tomb_id, survivor_id=survivor_id))

    assert _requires(primitives) == [(obl_id, new_id)]
    assert len(_capabilities(primitives)) == 1
