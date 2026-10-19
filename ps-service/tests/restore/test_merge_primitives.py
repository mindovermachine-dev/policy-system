"""`restore.merge_primitives`: the `policy_system` leg of a restore as primitives.

The leg is built in memory from reads made against the live graph at one log position, over an
overlay (ids at that position plus ids of the primitives built so far), and nothing is written.
These are the behaviours of an offline merge of the artifact's baseline, asserted on the
primitives. Digest equality on every curated artifact is in `test_restore_legs_equivalence.py`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._gateway_fakes import MergeRig, stepped_values
from company_merge._kinds import UNREGISTERED_KIND
from company_merge._seed_merge_graphs import SingleTenantStore

from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    CompanyMergeValidationError,
)
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.models import BareEdge, BaselineGraph, BaselineNode
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore import merge_primitives
from ps_service.restore.errors import ArtifactContentRejectedError, RestoreOwnerRequiredError
from ps_service.restore.merge_primitives import PolicySystemLeg, build_policy_system_leg

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_gateway.models import Primitive

_INSTRUMENT = "REG-1.0"
_ROLE = "role_1"
_REQUIREMENT = "REG-1.0_req_1"
_OBLIGATION = obligation_id(_ROLE, "Do the thing.")
_SURVIVOR = obligation_id(_ROLE, "Do the thing")
_CAP = capability_id("Thing Doing")
_OWNER = ("sub-owner", "https://idp.example")
_THRESHOLD = 0.85


def _node(label: str, node_id: str, **properties: object) -> SerializedNode:
    return SerializedNode(label=label, properties={"id": node_id, **properties})  # pyright: ignore[reportArgumentType]


def _edge(
    kind: str, source: tuple[str, str], target: tuple[str, str], **properties: object
) -> SerializedEdge:
    return SerializedEdge(
        relationship_type=kind,
        source_label=source[0],
        source_id=source[1],
        target_label=target[0],
        target_id=target[1],
        properties=dict(properties),  # pyright: ignore[reportArgumentType]
    )


def _external(
    *extra_nodes: SerializedNode, edges: tuple[SerializedEdge, ...] = ()
) -> SerializedGraph:
    """One Role, one Requirement, one Obligation requiring one Capability."""
    instrument = ("RegulatoryInstrument", _INSTRUMENT)
    return SerializedGraph(
        nodes=(
            _node("RegulatoryInstrument", _INSTRUMENT, title="Reg", source_type="external"),
            _node("Role", _ROLE, name="Operator", confidence=0.9),
            _node(
                "Requirement",
                _REQUIREMENT,
                text="Must",
                type="requirement",
                confidence=0.9,
                role_id=_ROLE,
            ),
            _node("Obligation", _OBLIGATION, text="Do the thing.", confidence=0.9),
            _node("Capability", _CAP, name="Thing Doing", confidence=0.8),
            *extra_nodes,
        ),
        edges=(
            _edge("DEFINES", instrument, ("Role", _ROLE), source_ref="art. 1"),
            _edge("EXPRESSES", instrument, ("Requirement", _REQUIREMENT), source_ref="art. 1.1"),
            _edge("HAS", ("Role", _ROLE), ("Obligation", _OBLIGATION)),
            _edge("SATISFIED_BY", ("Requirement", _REQUIREMENT), ("Obligation", _OBLIGATION)),
            _edge("REQUIRES", ("Obligation", _OBLIGATION), ("Capability", _CAP)),
            *edges,
        ),
    )


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


@pytest.fixture
def rig() -> Iterator[MergeRig]:
    merge_rig = MergeRig()
    yield merge_rig
    merge_rig.close()


def _build(
    live: GraphHandle,
    baseline: SerializedGraph,
    *,
    governance: GovernanceImport | None = None,
) -> PolicySystemLeg:
    return build_policy_system_leg(
        baseline,
        instrument_id=_INSTRUMENT,
        live=live,
        governance=governance or GovernanceImport(owner=_OWNER),
        similarity_threshold=_THRESHOLD,
        emitter=None,
        values=stepped_values(),
    )


def _nodes(leg: PolicySystemLeg, label: str) -> list[UpsertNode]:
    return [p for p in leg.primitives if isinstance(p, UpsertNode) and p.label == label]


def _edges(leg: PolicySystemLeg, edge_type: str) -> list[UpsertEdge]:
    return [p for p in leg.primitives if isinstance(p, UpsertEdge) and p.type == edge_type]


def _first(primitives: tuple[Primitive, ...], label: str) -> int:
    return next(
        i for i, p in enumerate(primitives) if isinstance(p, UpsertNode) and p.label == label
    )


def test_a_new_instrument_is_built_in_the_old_write_order(rig: MergeRig) -> None:
    leg = _build(rig.legacy, _external())

    order = [
        _first(leg.primitives, label) for label in ("RegulatoryInstrument", "Role", "Requirement")
    ]
    assert order == sorted(order)
    assert _first(leg.primitives, "Role") < _first(leg.primitives, "Obligation")
    assert _first(leg.primitives, "Obligation") < _first(leg.primitives, "Capability")
    first_rewired = next(
        i for i, p in enumerate(leg.primitives)
        if isinstance(p, UpsertEdge) and p.type in {"HAS", "SATISFIED_BY", "REQUIRES"}
    )  # fmt: skip
    assert _first(leg.primitives, "Capability") < first_rewired


def test_the_instrument_is_refreshed_without_its_id_in_the_property_bag(rig: MergeRig) -> None:
    (instrument,) = _nodes(_build(rig.legacy, _external()), "RegulatoryInstrument")

    assert instrument.id == _INSTRUMENT
    assert instrument.properties == {"title": "Reg", "source_type": "external"}


def test_provenance_edges_carry_their_source_ref(rig: MergeRig) -> None:
    leg = _build(rig.legacy, _external())

    (defines,) = _edges(leg, "DEFINES")
    (expresses,) = _edges(leg, "EXPRESSES")
    assert (defines.identity, defines.properties) == (
        f"DEFINES:{_INSTRUMENT}->{_ROLE}",
        {"source_ref": "art. 1"},
    )
    assert expresses.properties == {"source_ref": "art. 1.1"}


def test_edges_between_nodes_this_leg_creates_are_built_though_the_graph_lacks_them(
    rig: MergeRig,
) -> None:
    """The own-write reads: endpoints written by this very leg are known through the overlay."""
    leg = _build(rig.legacy, _external())

    assert rig.policy_system.nodes == {}
    assert {e.type for e in leg.primitives if isinstance(e, UpsertEdge)} >= {
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
    }


def test_an_edge_whose_endpoint_exists_nowhere_is_dropped(rig: MergeRig) -> None:
    baseline = _external(edges=(_edge("HAS", ("Role", _ROLE), ("Obligation", "ob_ghost")),))

    leg = _build(rig.legacy, baseline)

    assert all(e.identity != f"HAS:{_ROLE}->ob_ghost" for e in _edges(leg, "HAS"))


def test_role_requirement_and_obligation_the_graph_holds_are_create_only(rig: MergeRig) -> None:
    rig.policy_system.seed_node(("Role",), _ROLE, {"name": "Existing operator"})
    rig.policy_system.seed_node(("Obligation",), _OBLIGATION, {"text": "kept", "confidence": 0.1})

    leg = _build(rig.legacy, _external())

    assert _nodes(leg, "Role") == []
    assert _nodes(leg, "Obligation") == []
    assert _nodes(leg, "Requirement") != []
    assert [e.identity for e in _edges(leg, "HAS")] == [f"HAS:{_ROLE}->{_OBLIGATION}"]


def test_a_capability_the_graph_already_holds_is_not_minted_and_the_edge_reaches_it(
    rig: MergeRig,
) -> None:
    rig.policy_system.seed_node(("Capability",), _CAP, {"name": "Thing Doing", "confidence": 0.7})

    leg = _build(rig.legacy, _external())

    assert _nodes(leg, "Capability") == []
    assert [e.identity for e in _edges(leg, "REQUIRES")] == [f"REQUIRES:{_OBLIGATION}->{_CAP}"]


def test_a_new_capability_is_minted_with_created_at_and_no_comparison_embedding(
    rig: MergeRig,
) -> None:
    (capability,) = _nodes(_build(rig.legacy, _external()), "Capability")

    assert capability.id == _CAP
    assert capability.properties["name"] == "Thing Doing"
    assert "created_at" in capability.properties


def test_nothing_is_written_to_the_graph_it_reads(rig: MergeRig) -> None:
    before = (dict(rig.policy_system.nodes), dict(rig.policy_system.edges))

    _build(rig.legacy, _external())

    assert (dict(rig.policy_system.nodes), dict(rig.policy_system.edges)) == before
    assert rig.groups == []


def test_the_counts_are_the_classification_and_governance_audit_keys(rig: MergeRig) -> None:
    counts = _build(rig.legacy, _external()).counts

    assert counts == {
        "practice_area_count": 0,
        "risk_path_count": 0,
        "covers_count": 0,
        "owns_count": 0,
        "mitigated_by_count": 0,
        "verified_by_count": 0,
        "governance_policies": 0,
        "governance_standards": 0,
        "governance_controls": 0,
        "governance_status_overridden": 0,
        "near_miss_reviews_created": 0,
    }


def _near_miss_graph() -> SerializedGraph:
    """The baseline of `_external` with an embedding scoring 0.8 against the seeded Capability."""
    graph = _external()
    nodes = tuple(
        _node("Capability", _CAP, name="Thing Doing", confidence=0.8, embedding=[0.8, 0.6])
        if n.label == "Capability"
        else n
        for n in graph.nodes
    )
    return SerializedGraph(nodes=nodes, edges=graph.edges)


def _seed_near_miss_target(rig: MergeRig) -> str:
    existing = capability_id("Existing Thing")
    rig.policy_system.seed_node(
        ("Capability",),
        existing,
        {"id": existing, "name": "Existing Thing", "embedding": [1.0, 0.0], "status": "active"},
    )
    return existing


def test_AC_BI_001_near_misses_become_pending_review_nodes(rig: MergeRig) -> None:
    existing = _seed_near_miss_target(rig)

    leg = _build(rig.legacy, _near_miss_graph())

    (review,) = _nodes(leg, "PendingReview")
    assert review.properties["kind"] == "Capability"
    assert review.properties["incoming_id"] == _CAP
    assert review.properties["incoming_text"] == "Thing Doing"
    assert review.properties["nearest_existing_id"] == existing
    assert review.properties["nearest_existing_text"] == "Existing Thing"
    assert review.properties["similarity"] == pytest.approx(0.8)
    assert leg.counts["near_miss_reviews_created"] == 1


def test_reviews_are_last_in_the_leg_and_nothing_is_written_to_the_graph(rig: MergeRig) -> None:
    _seed_near_miss_target(rig)
    before = (dict(rig.policy_system.nodes), dict(rig.policy_system.edges))

    leg = _build(rig.legacy, _near_miss_graph())

    kinds = [isinstance(p, UpsertNode) and p.label == "PendingReview" for p in leg.primitives]
    assert kinds[-1]
    assert kinds.index(True) == len(kinds) - kinds.count(True)
    assert (dict(rig.policy_system.nodes), dict(rig.policy_system.edges)) == before
    assert rig.groups == []


def _seed_review(
    rig: MergeRig, *, incoming_id: str, nearest_existing_id: str, kind: str = "Capability"
) -> None:
    rig.policy_system.nodes[("PendingReview", "review_seeded")] = {
        "id": "review_seeded",
        "kind": kind,
        "status": "pending",
        "incoming_id": incoming_id,
        "nearest_existing_id": nearest_existing_id,
    }


def _review_reads(rig: MergeRig) -> list[str]:
    return [q for q in rig.legacy.statements if "PendingReview" in q]


def test_AC_BI_004_pair_with_unresolved_review_is_skipped(rig: MergeRig) -> None:
    existing = _seed_near_miss_target(rig)
    _seed_review(rig, incoming_id=_CAP, nearest_existing_id=existing)

    leg = _build(rig.legacy, _near_miss_graph())

    assert _review_reads(rig), "the unresolved-review read was never issued"
    assert _nodes(leg, "PendingReview") == []
    assert leg.counts["near_miss_reviews_created"] == 0


def test_AC_BI_004_a_review_of_another_pair_or_kind_does_not_skip(rig: MergeRig) -> None:
    existing = _seed_near_miss_target(rig)
    _seed_review(rig, incoming_id=_CAP, nearest_existing_id="cap_other")
    rig.policy_system.nodes[("PendingReview", "review_other_kind")] = {
        "id": "review_other_kind",
        "kind": UNREGISTERED_KIND,
        "status": "pending",
        "incoming_id": _CAP,
        "nearest_existing_id": existing,
    }

    leg = _build(rig.legacy, _near_miss_graph())

    assert _review_reads(rig)
    assert len(_nodes(leg, "PendingReview")) == 1
    assert leg.counts["near_miss_reviews_created"] == 1


def test_a_leg_without_near_misses_issues_no_review_read(rig: MergeRig) -> None:
    _build(rig.legacy, _external())

    assert _review_reads(rig) == []


def test_a_leg_without_near_misses_creates_no_review_and_counts_zero(rig: MergeRig) -> None:
    leg = _build(rig.legacy, _external())

    assert _nodes(leg, "PendingReview") == []
    assert leg.counts["near_miss_reviews_created"] == 0


def _internal() -> SerializedGraph:
    base = _external()
    policy = ("Policy", "pol_1")
    standard = ("Standard", "std_1")
    control = ("Control", "ctl_1")
    area = ("PracticeArea", "pa_1")
    risk = ("RiskPath", "rp_1")
    return SerializedGraph(
        nodes=(
            *base.nodes,
            _node("Policy", "pol_1", title="P", status="approved", embedding=[0.5, 0.25]),
            _node("Standard", "std_1", title="S", status="approved"),
            _node("Control", "ctl_1", title="C"),
            _node("PracticeArea", "pa_1", name="Privacy", status="active"),
            _node("RiskPath", "rp_1", name="Leak", status="active"),
        ),
        edges=(
            *base.edges,
            _edge("GOVERNED_BY", ("Capability", _CAP), policy),
            _edge("SUPPORTED_BY", policy, standard),
            _edge("IMPLEMENTED_BY", standard, control),
            _edge("COVERS", area, ("Capability", _CAP)),
            _edge("OWNS", area, policy),
            _edge("MITIGATED_BY", risk, ("Capability", _CAP)),
            _edge("VERIFIED_BY", risk, control),
        ),
    )


def test_a_governance_tree_is_built_as_draft_owned_by_the_caller(rig: MergeRig) -> None:
    leg = _build(
        rig.legacy,
        _internal(),
        governance=GovernanceImport(owner=_OWNER, policy_embeddings={"pol_1": (0.5, 0.25)}),
    )

    (policy,) = _nodes(leg, "Policy")
    assert policy.properties["status"] == "draft"
    assert (policy.properties["owner_subject"], policy.properties["owner_issuer"]) == _OWNER
    assert policy.embedding == (0.5, 0.25)
    assert {p.id for p in _nodes(leg, "Standard")} == {"std_1"}
    assert _nodes(leg, "Standard")[0].properties["status"] == "draft"
    assert leg.counts["governance_policies"] == 1


def test_an_existing_policy_is_never_overwritten_or_converged_onto(rig: MergeRig) -> None:
    rig.policy_system.seed_node(
        ("Policy",), "pol_1", {"title": "Customer policy", "status": "approved"}
    )

    leg = _build(rig.legacy, _internal())

    assert _nodes(leg, "Policy") == []
    assert [e.identity for e in _edges(leg, "SUPPORTED_BY")] == ["SUPPORTED_BY:pol_1->std_1"]


def test_governance_content_without_an_owner_is_refused_before_any_read() -> None:
    class _NoReads:
        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            raise AssertionError(f"a read reached the graph: {q}")

    with pytest.raises(RestoreOwnerRequiredError):
        build_policy_system_leg(
            _internal(),
            instrument_id=_INSTRUMENT,
            live=cast("GraphHandle", _NoReads()),
            governance=GovernanceImport(owner=None),
            similarity_threshold=_THRESHOLD,
            emitter=None,
            values=stepped_values(),
        )


def test_classification_nodes_pass_through_and_edges_are_rewired(rig: MergeRig) -> None:
    leg = _build(rig.legacy, _internal())

    assert {p.id for p in _nodes(leg, "PracticeArea")} == {"pa_1"}
    assert {p.id for p in _nodes(leg, "RiskPath")} == {"rp_1"}
    assert [e.identity for e in _edges(leg, "COVERS")] == [f"COVERS:pa_1->{_CAP}"]
    assert [e.identity for e in _edges(leg, "VERIFIED_BY")] == ["VERIFIED_BY:rp_1->ctl_1"]
    assert leg.counts["covers_count"] == 1
    assert leg.counts["practice_area_count"] == 1


def test_a_classification_edge_with_a_missing_endpoint_fails_the_whole_leg(
    rig: MergeRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable from a folded artifact (a dangling edge is dropped first): the read is faked."""
    real = merge_primitives.graph_reader.read_baseline_graph

    def read_with_dangling_edge(graph: GraphHandle, instrument_id: str) -> BaselineGraph:
        found = real(graph, instrument_id)
        ghost = BareEdge(relationship_type="VERIFIED_BY", source_id="rp_1", target_id="ctl_ghost")
        return replace(
            found,
            risk_path_nodes=(BaselineNode(id="rp_1", properties={"name": "Leak", "status": "a"}),),
            classification_edges=(ghost,),
        )

    # detroit-exception: dangling edge is unreachable from a real graph; the read is the seam
    monkeypatch.setattr(
        merge_primitives.graph_reader, "read_baseline_graph", read_with_dangling_edge
    )

    with pytest.raises(CompanyMergePersistenceError, match="ctl_ghost"):
        _build(rig.legacy, _external())


class _MarkedLive(SingleTenantStore):
    """A `policy_system` read handle holding one `MergedObligation` marker."""

    def __init__(self, *, survivor_exists: bool = True) -> None:
        super().__init__()
        if survivor_exists:
            self.nodes[("Obligation", _SURVIVOR)] = {"text": "Do the thing"}

    def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
        if "(m:MergedObligation)" in q:
            return [[_OBLIGATION, _SURVIVOR]]
        return super()._read(q, params)


def test_an_obligation_a_cleanup_merge_absorbed_is_not_minted_and_its_edges_move() -> None:
    leg = _build(_MarkedLive(), _external())

    assert _nodes(leg, "Obligation") == []
    assert [e.identity for e in _edges(leg, "HAS")] == [f"HAS:{_ROLE}->{_SURVIVOR}"]
    assert [e.identity for e in _edges(leg, "SATISFIED_BY")] == [
        f"SATISFIED_BY:{_REQUIREMENT}->{_SURVIVOR}"
    ]
    assert [e.identity for e in _edges(leg, "REQUIRES")] == [f"REQUIRES:{_SURVIVOR}->{_CAP}"]


def test_a_vanished_survivor_fails_the_leg_closed() -> None:
    with pytest.raises(CompanyMergeValidationError):
        _build(_MarkedLive(survivor_exists=False), _external())


def test_a_manifest_id_matching_no_instrument_in_the_artifact_is_rejected(rig: MergeRig) -> None:
    with pytest.raises(ArtifactContentRejectedError, match="RegulatoryInstrument"):
        build_policy_system_leg(
            _external(),
            instrument_id="OTHER-1.0",
            live=rig.legacy,
            governance=GovernanceImport(owner=_OWNER),
            similarity_threshold=_THRESHOLD,
            emitter=None,
            values=stepped_values(),
        )


def test_a_baseline_without_any_instrument_is_a_legitimate_empty_leg(rig: MergeRig) -> None:
    leg = _build(rig.legacy, SerializedGraph(nodes=(), edges=()))

    assert not any(
        isinstance(p, UpsertNode) and p.label in {"Role", "Requirement", "Obligation"}
        for p in leg.primitives
    )
