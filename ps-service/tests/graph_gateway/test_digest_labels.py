"""A node's label set is part of the digest (#207 S5, AC-RD-002)."""

from __future__ import annotations

from graph_gateway._fakes import InMemoryGraph
from ps_service.graph_gateway.digest import canonical_digest


def _graph(*nodes: tuple[tuple[str, ...], str, dict[str, object]]) -> InMemoryGraph:
    graph = InMemoryGraph()
    for labels, node_id, properties in nodes:
        graph.seed_node(labels, node_id, properties)
    return graph


def test_digest_differs_when_a_node_carries_an_extra_label() -> None:
    one = _graph((("Capability",), "x", {"name": "a"}))
    two = _graph((("Capability", "Draft"), "x", {"name": "a"}))

    assert canonical_digest(one) != canonical_digest(two)


def test_digest_equal_for_the_same_labels_in_a_different_order() -> None:
    first = _graph((("Capability", "Draft"), "x", {"name": "a"}))
    second = _graph((("Draft", "Capability"), "x", {"name": "a"}))

    assert canonical_digest(first) == canonical_digest(second)


def test_digest_covers_a_label_outside_the_allow_list() -> None:
    plain = _graph((("Capability",), "x", {"name": "a"}))
    odd = _graph((("NotInTheAllowList",), "x", {"name": "a"}))

    assert canonical_digest(odd) != canonical_digest(plain)


def test_digest_differs_when_only_the_label_differs_between_same_id_nodes() -> None:
    policy = _graph((("Policy",), "x", {}))
    standard = _graph((("Standard",), "x", {}))

    assert canonical_digest(policy) != canonical_digest(standard)


def test_a_label_set_is_not_confused_with_a_single_longer_label() -> None:
    split = _graph((("AB", "C"), "x", {}))
    joined = _graph((("A", "BC"), "x", {}))

    assert canonical_digest(split) != canonical_digest(joined)
