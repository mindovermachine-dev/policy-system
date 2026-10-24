"""The split as pure functions: the edge rule, the validation, the preview and the state digest."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    BaselineCapability,
    CapabilityNodeState,
    EdgeRecord,
    GoverningPolicy,
)
from ps_service.graph_cleanup.split_planner import (
    CapabilitySplitState,
    HeldEdges,
    SplitHolder,
    held_edges,
    map_obligation_sources,
    plan_capability_split,
    split_digest_payload,
)
from ps_service.graph_cleanup.state_digest import canonical_digest

_X = "cap_x"
_A = "cap_a"
_ALIAS = "cap_a_alias"


def test_held_on_x_needs_an_existing_mapped_source_and_held_on_a_counts_every_alias() -> None:
    held = held_edges(
        (
            ("obl_1", _X),
            ("obl_1", _X),
            ("obl_absorbed", _X),
            ("obl_gone", _X),
            ("obl_2", _A),
            ("obl_3", _ALIAS),
            ("obl_4", "cap_unrelated"),
        ),
        x_id=_X,
        aliases=(_A, _ALIAS),
        source_ids={"obl_absorbed": "obl_survivor"},
        existing_sources=("obl_1", "obl_survivor", "obl_2", "obl_3", "obl_4"),
    )

    assert held.held_x == frozenset({"obl_1", "obl_survivor"})
    assert held.held_a == frozenset({"obl_2", "obl_3"})
    assert held.skipped_missing == frozenset({"obl_gone"})


def test_x_is_never_an_alias_of_a() -> None:
    held = held_edges(
        (("obl_1", _X),),
        x_id=_X,
        aliases=(_A, _X),
        source_ids={},
        existing_sources=("obl_1",),
    )

    assert (held.held_x, held.held_a) == (frozenset({"obl_1"}), frozenset())


def test_obligation_sources_follow_their_marker_chain_to_the_terminal() -> None:
    mapping = map_obligation_sources(
        ("obl_a", "obl_b", "obl_c"), {"obl_a": "obl_b", "obl_b": "obl_c"}
    )

    assert mapping == {"obl_a": "obl_c", "obl_b": "obl_c", "obl_c": "obl_c"}


def test_an_obligation_marker_cycle_is_a_validation_error() -> None:
    with pytest.raises(GraphCleanupValidationError, match="cycle"):
        map_obligation_sources(("obl_a",), {"obl_a": "obl_b", "obl_b": "obl_a"})


def _node(capability_id: str, *, status: str = "active") -> CapabilityNodeState:
    return CapabilityNodeState(id=capability_id, name=capability_id, status=status, properties={})


def _held(
    held_x: set[str], held_a: set[str] | None = None, skipped: set[str] | None = None
) -> HeldEdges:
    return HeldEdges(
        held_x=frozenset(held_x),
        held_a=frozenset(held_a or set()),
        skipped_missing=frozenset(skipped or set()),
    )


def _on_a(rel_type: str, source_label: str, source: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type=rel_type,
        source_label=source_label,
        source_id=source,
        target_label="Capability",
        target_id=_A,
    )


def _state(**changes: object) -> CapabilitySplitState:
    base = CapabilitySplitState(
        x_id=_X,
        x_marker_label="ResolvedCapability",
        marker_resolves_to=_A,
        chain=(_X, _A),
        x_is_capability=False,
        terminal=_node(_A),
        terminal_policies=(GoverningPolicy(id="pol_1", title="Policy", status="approved"),),
        terminal_edges=(
            _on_a("REQUIRES", "Obligation", "obl_moves"),
            _on_a("REQUIRES", "Obligation", "obl_shared"),
            _on_a("REQUIRES", "Obligation", "obl_stays"),
            _on_a("COVERS", "PracticeArea", "pa_moves"),
        ),
        holders=(
            SplitHolder(
                graph_name="nis2_baseline",
                node=BaselineCapability(id=_X, name="X", embedding=(1.0, 0.0)),
                instruments=("32022L2555",),
            ),
        ),
        held={
            "REQUIRES": _held({"obl_moves", "obl_shared", "obl_added"}, {"obl_shared"}),
            "COVERS": _held({"pa_moves"}),
        },
    )
    return replace(base, **changes)


def test_each_source_edge_moves_is_shared_is_added_or_stays_and_governance_never_moves() -> None:
    plan = plan_capability_split(_state())

    requires = plan.preview.edges.requires
    assert (requires.moves, requires.shared, requires.added, requires.stays) == (
        ("obl_moves",),
        ("obl_shared",),
        ("obl_added",),
        ("obl_stays",),
    )
    assert plan.preview.edges.covers.moves == ("pa_moves",)
    after = {(e.rel_type, e.source_id, e.target_id) for e in plan.after.edges}
    assert ("GOVERNED_BY", _A, "pol_1") in after
    assert not any(e.rel_type == "GOVERNED_BY" and e.source_id == _X for e in plan.after.edges)
    assert ("REQUIRES", "obl_moves", _A) not in after
    assert {("REQUIRES", "obl_shared", _A), ("REQUIRES", "obl_shared", _X)} <= after
    assert ("Capability", _X) in {(n.label, n.id) for n in plan.after.nodes}
    assert ("ResolvedCapability", _X) not in {(n.label, n.id) for n in plan.after.nodes}
    assert plan.preview.governance is not None
    assert plan.preview.governance.acknowledgment_required is True
    assert plan.preview.obligations_moving == 1


def test_the_source_baseline_is_the_first_holder_with_an_embedding() -> None:
    holders = (
        SplitHolder(
            graph_name="a_baseline", node=BaselineCapability(id=_X, name="x"), instruments=()
        ),
        SplitHolder(
            graph_name="b_baseline",
            node=BaselineCapability(id=_X, name="X", embedding=(0.5,)),
            instruments=("i",),
        ),
    )

    plan = plan_capability_split(_state(holders=holders))

    assert (plan.preview.source_baseline, plan.minted.name) == ("b_baseline", "X")


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"x_is_capability": True}, "already exists as a Capability"),
        ({"x_marker_label": "MergedCapability"}, "merged by a cleanup merge"),
        ({"x_marker_label": None, "marker_resolves_to": None}, "no automatic resolution"),
        ({"terminal": None}, "not an existing Capability"),
        ({"holders": ()}, "no regulation baseline"),
    ],
)
def test_a_split_that_must_never_get_an_approval_is_refused(
    changes: dict[str, object], fragment: str
) -> None:
    with pytest.raises(GraphCleanupValidationError, match=fragment):
        plan_capability_split(_state(**changes))


_DIGEST_MUTATIONS: dict[str, dict[str, object]] = {
    "x_id": {"x_id": "cap_other"},
    "marker_resolves_to": {"marker_resolves_to": "cap_d"},
    "chain": {"chain": (_X, "cap_d", _A)},
    "a_id": {"chain": (_X, "cap_b")},
    "a_status": {"terminal": _node(_A, status="deprecated")},
    "a_governors": {"terminal_policies": ()},
    "x_is_capability": {"x_is_capability": True},
    "x_is_merged": {"x_marker_label": "MergedCapability"},
    "edges": {"held": {"REQUIRES": _held({"obl_moves"})}},
    "source_baseline": {
        "holders": (
            SplitHolder(
                graph_name="other_baseline",
                node=BaselineCapability(id=_X, name="X", embedding=(1.0, 0.0)),
                instruments=("32022L2555",),
            ),
        )
    },
    "source_name": {
        "holders": (
            SplitHolder(
                graph_name="nis2_baseline",
                node=BaselineCapability(id=_X, name="Y", embedding=(1.0, 0.0)),
                instruments=("32022L2555",),
            ),
        )
    },
    "source_embedding_sha256": {
        "holders": (
            SplitHolder(
                graph_name="nis2_baseline",
                node=BaselineCapability(id=_X, name="X", embedding=(0.0, 1.0)),
                instruments=("32022L2555",),
            ),
        )
    },
}


def test_the_digest_payload_has_exactly_the_bound_fields() -> None:
    assert set(split_digest_payload(_state())) == set(_DIGEST_MUTATIONS)


@pytest.mark.parametrize("key", sorted(_DIGEST_MUTATIONS))
def test_each_single_field_change_of_the_state_changes_the_digest(key: str) -> None:
    base = split_digest_payload(_state())
    mutated = split_digest_payload(_state(**_DIGEST_MUTATIONS[key]))

    assert mutated[key] != base[key]
    assert canonical_digest(mutated) != canonical_digest(base)


def test_the_digest_is_stable_for_an_equal_state() -> None:
    assert (
        plan_capability_split(_state()).state_digest == plan_capability_split(_state()).state_digest
    )
