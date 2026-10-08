"""Profiles derive a narrower schema from the base one (AC-BI-004, unit level)."""

from __future__ import annotations

import pytest

from ps_service.domain_schema import (
    Add,
    Cardinality,
    ConstType,
    Edge,
    EnumType,
    Narrow,
    Node,
    Omit,
    Presence,
    Profile,
    Property,
    Schema,
    SchemaProfileError,
    StringType,
    apply_profile,
    render_slim_schema,
)

_STATUS = EnumType(("draft", "proposed", "approved"))
_ITEM = Node(
    "Item",
    (
        Property("name", StringType(), Presence.REQUIRED),
        Property("status", _STATUS, Presence.REQUIRED),
        Property("nickname", StringType(), Presence.OPTIONAL),
    ),
)
_OTHER = Node("Other", (Property("kind", _STATUS, Presence.REQUIRED),))
_LINK = Edge("LINKS", "Item", "Other", Cardinality.parse("1 : 0..*"))
_BACK = Edge("BACKS", "Other", "Item", Cardinality.parse("1 : 0..*"))
_BASE = Schema(nodes=(_ITEM, _OTHER), edges=(_LINK, _BACK))


def _profile(
    *operations: Omit | Narrow | Add,
    node_order: tuple[str, ...] | None = None,
    edge_order: tuple[tuple[str, str, str], ...] | None = None,
    omitted_edges: tuple[tuple[str, str, str], ...] = (),
) -> Profile:
    return Profile(
        name="test",
        operations=operations,
        node_order=node_order,
        edge_order=edge_order,
        omitted_edges=omitted_edges,
    )


def _item(schema: Schema) -> Node:
    return next(node for node in schema.nodes if node.label == "Item")


def test_omit_removes_a_property() -> None:
    result = apply_profile(_BASE, _profile(Omit("Item", "nickname")))

    assert [p.name for p in _item(result).properties] == ["name", "status"]


def test_add_appends_a_property_at_the_end() -> None:
    added = Property("score", StringType(), Presence.OPTIONAL)

    result = apply_profile(_BASE, _profile(Add("Item", added)))

    assert [p.name for p in _item(result).properties] == ["name", "status", "nickname", "score"]


def test_narrow_enum_to_a_subset_applies() -> None:
    narrowed = EnumType(("draft", "approved"))

    result = apply_profile(_BASE, _profile(Narrow("Item", "status", narrowed)))

    status = next(p for p in _item(result).properties if p.name == "status")
    assert status.type == narrowed
    assert status.presence is Presence.REQUIRED


def test_narrow_enum_to_a_base_value_constant_applies() -> None:
    result = apply_profile(_BASE, _profile(Narrow("Item", "status", ConstType("draft"))))

    status = next(p for p in _item(result).properties if p.name == "status")
    assert status.type == ConstType("draft")


def test_narrow_string_may_raise_min_length() -> None:
    result = apply_profile(_BASE, _profile(Narrow("Item", "name", StringType(min_length=1))))

    assert next(p for p in _item(result).properties if p.name == "name").type == StringType(1)


def test_narrow_to_a_value_outside_the_base_enum_raises() -> None:
    with pytest.raises(SchemaProfileError, match="rejected"):
        apply_profile(_BASE, _profile(Narrow("Item", "status", EnumType(("draft", "rejected")))))


def test_narrow_to_a_constant_outside_the_base_enum_raises() -> None:
    with pytest.raises(SchemaProfileError, match="rejected"):
        apply_profile(_BASE, _profile(Narrow("Item", "status", ConstType("rejected"))))


def test_narrow_string_lowering_min_length_raises() -> None:
    base = Schema(nodes=(Node("Item", (Property("name", StringType(2), Presence.REQUIRED),)),))

    with pytest.raises(SchemaProfileError, match="name"):
        apply_profile(base, _profile(Narrow("Item", "name", StringType(min_length=1))))


def test_narrow_across_unrelated_types_raises() -> None:
    with pytest.raises(SchemaProfileError, match="status"):
        apply_profile(_BASE, _profile(Narrow("Item", "status", StringType())))


def test_narrow_of_an_unknown_property_raises() -> None:
    with pytest.raises(SchemaProfileError, match="ghost"):
        apply_profile(_BASE, _profile(Narrow("Item", "ghost", StringType())))


def test_omit_of_an_unknown_property_raises() -> None:
    with pytest.raises(SchemaProfileError, match="ghost"):
        apply_profile(_BASE, _profile(Omit("Item", "ghost")))


def test_operation_on_an_unknown_label_raises() -> None:
    with pytest.raises(SchemaProfileError, match="Ghost"):
        apply_profile(_BASE, _profile(Omit("Ghost", "name")))


def test_add_of_an_existing_name_raises() -> None:
    with pytest.raises(SchemaProfileError, match="name"):
        apply_profile(
            _BASE, _profile(Add("Item", Property("name", StringType(), Presence.OPTIONAL)))
        )


def test_omit_then_add_relaxes_a_property() -> None:
    """The profile expression of 'relax required': drop the property, add it back optional."""
    relaxed = Property("name", StringType(), Presence.OPTIONAL)

    result = apply_profile(_BASE, _profile(Omit("Item", "name"), Add("Item", relaxed)))

    assert [(p.name, p.presence) for p in _item(result).properties][-1] == (
        "name",
        Presence.OPTIONAL,
    )


def test_node_order_override_reorders_nodes() -> None:
    result = apply_profile(_BASE, _profile(node_order=("Other", "Item")))

    assert [node.label for node in result.nodes] == ["Other", "Item"]


def test_node_order_that_is_not_a_permutation_raises() -> None:
    with pytest.raises(SchemaProfileError, match="node order"):
        apply_profile(_BASE, _profile(node_order=("Other",)))


def test_edge_order_override_reorders_edges() -> None:
    result = apply_profile(_BASE, _profile(edge_order=(_BACK.key, _LINK.key)))

    assert [edge.key for edge in result.edges] == [_BACK.key, _LINK.key]


def test_edge_order_that_is_not_a_permutation_raises() -> None:
    with pytest.raises(SchemaProfileError, match="edge order"):
        apply_profile(_BASE, _profile(edge_order=(_BACK.key, _BACK.key)))


def test_omitted_edges_are_dropped_before_order_is_checked() -> None:
    result = apply_profile(_BASE, _profile(omitted_edges=(_BACK.key,), edge_order=(_LINK.key,)))

    assert [edge.key for edge in result.edges] == [_LINK.key]


def test_omitting_an_unknown_edge_raises() -> None:
    with pytest.raises(SchemaProfileError, match="GHOST"):
        apply_profile(_BASE, _profile(omitted_edges=(("GHOST", "Item", "Other"),)))


def test_applying_a_profile_leaves_the_base_schema_unchanged() -> None:
    apply_profile(_BASE, _profile(Omit("Item", "nickname")))

    assert [p.name for p in _item(_BASE).properties] == ["name", "status", "nickname"]


def test_profiled_schema_renders_through_the_slim_renderer() -> None:
    """Vertical: a narrowed enum and a constant show up in the slim output."""
    profile = _profile(
        Narrow("Item", "status", EnumType(("draft", "approved"))),
        Narrow("Other", "kind", ConstType("draft")),
    )

    text = render_slim_schema(apply_profile(_BASE, profile))

    assert "  status: enum(draft|approved) !" in text
    assert "  kind: const(draft) !" in text
