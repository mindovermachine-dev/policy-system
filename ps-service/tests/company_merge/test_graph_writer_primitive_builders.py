"""The pure builders `graph_writer` exposes for Restore.

Restore builds the `policy_system` leg of a restore as primitives in memory, so it needs the
decisions `persist_obligation_passthrough`, `persist_practice_area_and_risk_path_passthrough` and
`validate_classification_edge_endpoints` make as pure functions. They are extractions: the
`persist_*` functions call them, and their own tests (`test_graph_writer_passthrough.py`,
`test_graph_writer_classification_validation.py`) stay untouched.
"""

from __future__ import annotations

from ps_service.company_merge.graph_writer import (
    build_obligation_primitives,
    build_practice_area_and_risk_path_primitives,
    missing_classification_endpoints,
    unresolved_classification_endpoint_ids,
)
from ps_service.company_merge.models import BareEdge, BaselineNode
from ps_service.graph_gateway.models import UpsertNode


def _node(node_id: str, **properties: str | float) -> BaselineNode:
    return BaselineNode(id=node_id, properties=dict(properties))


def test_obligations_are_create_only_and_keep_the_first_occurrence() -> None:
    primitives = build_obligation_primitives(
        {"ob_existing"},
        (
            _node("ob_existing", text="old"),
            _node("ob_new", text="first"),
            _node("ob_new", text="second"),
        ),
    )

    assert primitives == (
        UpsertNode(label="Obligation", id="ob_new", properties={"text": "first"}),
    )


def test_practice_areas_precede_risk_paths_and_existing_ids_are_left_out() -> None:
    primitives = build_practice_area_and_risk_path_primitives(
        {"pa_existing"},
        {"rp_existing"},
        (_node("pa_existing", name="x"), _node("pa_new", name="y")),
        (_node("rp_new", name="z"), _node("rp_existing", name="w")),
    )

    assert [(p.label, p.id) for p in primitives] == [
        ("PracticeArea", "pa_new"),
        ("RiskPath", "rp_new"),
    ]


def test_no_classification_content_builds_nothing() -> None:
    assert build_practice_area_and_risk_path_primitives(set(), set(), (), ()) == ()


def test_only_endpoints_without_a_canonical_entry_are_unresolved() -> None:
    edges = (
        BareEdge(relationship_type="COVERS", source_id="pa_1", target_id="cap_in"),
        BareEdge(relationship_type="VERIFIED_BY", source_id="rp_1", target_id="ctl_1"),
    )

    assert unresolved_classification_endpoint_ids(edges, {"cap_in": "cap_canonical"}) == {
        "pa_1",
        "rp_1",
        "ctl_1",
    }


def test_missing_endpoints_are_the_unresolved_ones_that_nothing_knows() -> None:
    edges = (
        BareEdge(relationship_type="COVERS", source_id="pa_1", target_id="cap_in"),
        BareEdge(relationship_type="VERIFIED_BY", source_id="rp_1", target_id="ctl_1"),
    )

    missing = missing_classification_endpoints({"pa_1", "ctl_1"}, edges, {"cap_in": "cap_c"})

    assert missing == {"rp_1"}


def test_no_classification_edges_means_nothing_is_missing() -> None:
    assert missing_classification_endpoints(set(), (), {}) == set()
    assert missing_classification_endpoints({"anything"}, (), {}) == set()
