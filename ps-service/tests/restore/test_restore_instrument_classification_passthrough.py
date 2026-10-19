"""Offline restore-path parity for the PracticeArea/RiskPath/classification passthrough.

The restore's `policy_system` leg (`merge_primitives.build_policy_system_leg`) mirrors
`company_merge.merge.merge_baseline_graph`'s live-path classification pass exactly: after
Capability dedup and the governance import, PracticeArea/RiskPath nodes pass through create-only
(no dedup, no embedding call -- identity convergence is structural, content-hashed ids minted
upstream by `internal_seed`), every COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge endpoint is
validated before any classification edge is built, and `classification_edges` are folded into the
same rewired-edge build as the regulatory-spine and governance edges.

That leg is a tuple of gateway primitives built from reads of the live graph, so these
expectations are asserted on primitives. The live graph is a hand-written fake satisfying
`GraphHandle` structurally.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.models import BareEdge
from ps_service.domain_mapper.identity import practice_area_id
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore import merge_primitives
from ps_service.restore.merge_primitives import PolicySystemLeg, build_policy_system_leg
from restore._gateway_fakes import ScriptedLive

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import BaselineGraph
    from ps_service.export.models import SerializedPropertyValue

_THRESHOLD = 0.85
_CLASSIFICATION_LABELS = (
    "PracticeArea",
    "RiskPath",
    "COVERS",
    "OWNS",
    "MITIGATED_BY",
    "VERIFIED_BY",
)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _baseline(
    *,
    regulatory_instrument_id: str = "REG-1.0",
    nodes: Sequence[SerializedNode] = (),
    edges: Sequence[SerializedEdge] = (),
) -> SerializedGraph:
    """A minimal internal-sourced baseline carrying only the content a test gives it."""
    instrument = SerializedNode(
        "RegulatoryInstrument", {"id": regulatory_instrument_id, "title": "Test Regulation"}
    )
    return SerializedGraph(nodes=(instrument, *nodes), edges=tuple(edges))


def _practice_area(node_id: str, **properties: SerializedPropertyValue) -> SerializedNode:
    return SerializedNode(
        "PracticeArea", {"id": node_id, "name": "Secure SDLC", "status": "active", **properties}
    )


def _edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
    return SerializedEdge(kind, source[0], source[1], target[0], target[1], {})


def _build(
    baseline: SerializedGraph,
    live: ScriptedLive,
    *,
    instrument_id: str = "REG-1.0",
) -> PolicySystemLeg:
    return build_policy_system_leg(
        baseline,
        instrument_id=instrument_id,
        live=live,
        governance=GovernanceImport(),
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        emitter=None,
    )


def _nodes(leg: PolicySystemLeg, label: str) -> list[UpsertNode]:
    return [p for p in leg.primitives if isinstance(p, UpsertNode) and p.label == label]


def _edges(leg: PolicySystemLeg, edge_type: str) -> list[UpsertEdge]:
    return [p for p in leg.primitives if isinstance(p, UpsertEdge) and p.type == edge_type]


def test_practice_area_and_risk_path_nodes_pass_through_with_every_property() -> None:
    """PracticeArea/RiskPath nodes are built with every property."""
    pa_id = "pa_secure_sdlc_4a7c1d"
    rp_id = "rp_secure_build_release_d93f8a"
    baseline = _baseline(
        nodes=(
            _practice_area(
                pa_id,
                description="Secure development lifecycle practices",
                version="1.0",
                owner_id="role_ciso",
            ),
            SerializedNode(
                "RiskPath",
                {
                    "id": rp_id,
                    "name": "Secure Build & Release",
                    "status": "active",
                    "description": "Risks in the build/release pipeline",
                    "risk_type": "operational",
                    "version": "2.0",
                },
            ),
        )
    )

    leg = _build(baseline, ScriptedLive())

    (practice_area,) = _nodes(leg, "PracticeArea")
    (risk_path,) = _nodes(leg, "RiskPath")
    assert (practice_area.id, practice_area.properties) == (
        pa_id,
        {
            "name": "Secure SDLC",
            "status": "active",
            "description": "Secure development lifecycle practices",
            "version": "1.0",
            "owner_id": "role_ciso",
        },
    )
    assert (risk_path.id, risk_path.properties) == (
        rp_id,
        {
            "name": "Secure Build & Release",
            "status": "active",
            "description": "Risks in the build/release pipeline",
            "risk_type": "operational",
            "version": "2.0",
        },
    )


def test_practice_area_and_risk_path_untouched_when_the_node_already_exists() -> None:
    """An existing PracticeArea/RiskPath keeps its properties.

    Create-only: the incoming node shares the id but carries other values, and no primitive is
    built for it, so the live node is never overwritten.
    """
    baseline = _baseline(
        nodes=(
            _practice_area("pa_existing", description="incoming-should-not-apply"),
            SerializedNode(
                "RiskPath",
                {
                    "id": "rp_existing",
                    "name": "Secure Build & Release",
                    "status": "active",
                    "description": "incoming-should-not-apply",
                },
            ),
        )
    )
    live = ScriptedLive(existing={"PracticeArea": {"pa_existing"}, "RiskPath": {"rp_existing"}})

    leg = _build(baseline, live)

    assert _nodes(leg, "PracticeArea") == []
    assert _nodes(leg, "RiskPath") == []


def test_covers_edge_target_rewritten_to_canonical_capability_id_in_restore() -> None:
    """A COVERS edge's Capability target follows the dedup.

    The OFFLINE dedup (`resolve_capability_convergence_offline`) matches the incoming Capability
    to an already-existing canonical one through an artifact-supplied embedding, so the edge is
    built onto the canonical id and no Capability is minted. No embedding call of any kind is
    possible: the offline dedup never calls `route_embedding` and the PracticeArea passthrough
    takes no embedding caller.
    """
    existing_capability_id = "cap_existing_encrypt_data_at_rest"
    incoming_capability_id = "cap_incoming_protect_data_at_rest"
    pa_id = "pa_secure_sdlc_4a7c1d"
    shared_vector = [1.0, 0.0]
    baseline = _baseline(
        nodes=(
            SerializedNode(
                "Capability",
                {
                    "id": incoming_capability_id,
                    "name": "Protect Data At Rest Capability",
                    "confidence": 0.8,
                    "embedding": shared_vector,
                },
            ),
            _practice_area(pa_id),
        ),
        edges=(_edge("COVERS", ("PracticeArea", pa_id), ("Capability", incoming_capability_id)),),
    )

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return [[existing_capability_id, "Encrypt Data At Rest Capability", shared_vector]]
        return None

    live = ScriptedLive(existing={"Capability": {existing_capability_id}}, answer=answer)

    leg = _build(baseline, live)

    (covers,) = _edges(leg, "COVERS")
    assert (covers.source.id, covers.target.id) == (pa_id, existing_capability_id)
    assert _nodes(leg, "Capability") == []


def test_names_converge_on_one_content_hashed_id_across_two_restores() -> None:
    """Two restores authoring a PracticeArea of one name converge.

    Both baselines carry the id content-hashed from the name (where real convergence happens).
    The first restore builds it, the second finds it in the live graph and builds nothing for it;
    neither build reaches a dedup call for PracticeArea/RiskPath (the offline dedup
    supports only Capability and Policy, so a call would raise instead of finish).
    """
    shared_pa_id = practice_area_id("Secure SDLC")
    first = _build(
        _baseline(regulatory_instrument_id="REG-A", nodes=(_practice_area(shared_pa_id),)),
        ScriptedLive(),
        instrument_id="REG-A",
    )
    second = _build(
        _baseline(regulatory_instrument_id="REG-B", nodes=(_practice_area(shared_pa_id),)),
        ScriptedLive(existing={"PracticeArea": {shared_pa_id}}),
        instrument_id="REG-B",
    )

    assert [n.id for n in _nodes(first, "PracticeArea")] == [shared_pa_id]
    assert _nodes(second, "PracticeArea") == []


def test_missing_classification_edge_endpoint_raises_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An endpoint absent from the baseline and the live graph raises.

    Nothing is returned, so no classification edge reaches the gateway; the restore fails with
    the log empty (the all-or-nothing guarantee, checked end to end in `test_restore_gateway.py`).
    A parsed artifact cannot reach this state (a dangling edge is dropped when the artifact is
    folded), so the baseline read is the faked part, as in `test_merge_primitives.py`.
    """
    pa_id = "pa_secure_sdlc_4a7c1d"
    real = merge_primitives.graph_reader.read_baseline_graph

    def read_with_dangling_edge(graph: GraphHandle, instrument_id: str) -> BaselineGraph:
        found = real(graph, instrument_id)
        dangling = BareEdge("COVERS", pa_id, "cap_never_persisted_anywhere")
        return replace(found, classification_edges=(dangling,))

    # detroit-exception: dangling edge is unreachable from a real graph; the read is the seam
    monkeypatch.setattr(
        merge_primitives.graph_reader, "read_baseline_graph", read_with_dangling_edge
    )
    baseline = _baseline(nodes=(_practice_area(pa_id),))

    with pytest.raises(CompanyMergePersistenceError, match="cap_never_persisted_anywhere"):
        _build(baseline, ScriptedLive())


def test_external_baseline_restore_unaffected_by_classification_pass() -> None:
    """An external-sourced restore reads nothing classification-shaped."""
    baseline = _baseline(
        nodes=(
            SerializedNode(
                "Capability",
                {"id": "cap_no_governance", "name": "Some Capability", "confidence": 0.8},
            ),
        )
    )
    live = ScriptedLive()

    leg = _build(baseline, live)

    assert not any(label in q for q in live.queries for label in _CLASSIFICATION_LABELS)
    assert all(
        not (isinstance(p, UpsertNode) and p.label in {"PracticeArea", "RiskPath"})
        for p in leg.primitives
    )
