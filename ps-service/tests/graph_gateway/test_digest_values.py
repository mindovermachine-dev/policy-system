"""Property and embedding values are compared exactly by the digest (#207 S4, AC-RD-002)."""

from __future__ import annotations

import math

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest, encode_properties, encode_value
from ps_service.graph_gateway.errors import GraphDigestError
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "g"


def _digest_of(
    properties: dict[str, object] | None = None, embedding: tuple[float, ...] | None = None
) -> str:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(
                UpsertNode(
                    label="Capability", id="cap-1", properties=properties or {}, embedding=embedding
                ),
            ),
        )
    )
    return canonical_digest(rig.graphs.open(_GRAPH))


def test_digest_differs_when_one_property_value_differs() -> None:
    assert _digest_of({"name": "a", "weight": 2}) != _digest_of({"name": "a", "weight": 3})
    assert _digest_of({"name": "a"}) != _digest_of({"name": "b"})
    assert _digest_of({"tags": ["x", "y"]}) != _digest_of({"tags": ["x", "z"]})


def test_digest_differs_when_an_embedding_value_differs_by_one_ulp() -> None:
    base = (0.1, 0.30000000000000004, 1 / 3)
    nudged = (0.1, 0.30000000000000004, math.nextafter(1 / 3, 1.0))

    assert _digest_of(embedding=base) != _digest_of(embedding=nudged)
    assert _digest_of(embedding=base) == _digest_of(embedding=base)


def test_digest_tells_apart_embedding_values_the_reply_prints_alike() -> None:
    # FalkorDB prints both of these as 0.3 and both of these extremes as inf/1.79769313486232e308.
    assert _digest_of(embedding=(0.3,)) != _digest_of(embedding=(0.30000000000000004,))
    assert _digest_of(embedding=(1.7976931348623157e308,)) != _digest_of(
        embedding=(1.7976931348623155e308,)
    )


def test_digest_differs_between_positive_and_negative_zero() -> None:
    assert _digest_of(embedding=(0.0, 1.0)) != _digest_of(embedding=(-0.0, 1.0))
    assert _digest_of({"offset": 0.0}) != _digest_of({"offset": -0.0})


def test_digest_distinguishes_int_one_from_float_one() -> None:
    assert _digest_of({"n": 1}) != _digest_of({"n": 1.0})
    assert encode_value(1) != encode_value(1.0)


def test_digest_distinguishes_bool_true_from_int_one() -> None:
    assert _digest_of({"flag": True}) != _digest_of({"flag": 1})
    true, false, one, zero = (True, False, 1, 0)
    assert encode_value(true) != encode_value(one)
    assert encode_value(false) != encode_value(zero)


def test_digest_distinguishes_list_order() -> None:
    assert _digest_of({"tags": ["a", "b"]}) != _digest_of({"tags": ["b", "a"]})
    assert encode_value([1, 2]) != encode_value([2, 1])


def test_digest_equal_for_property_maps_with_different_key_order() -> None:
    assert encode_properties({"a": 1, "b": "x"}) == encode_properties({"b": "x", "a": 1})


def test_an_int_and_a_string_with_the_same_digits_differ() -> None:
    assert encode_value(12) != encode_value("12")
    assert encode_value("") != encode_value([])


def test_the_largest_integers_encode_distinctly() -> None:
    assert encode_value(2**63 - 1) != encode_value(-(2**63))


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, None, {"a": 1}, 2**63, b"x"])
def test_a_value_the_gateway_cannot_have_written_is_a_typed_error(value: object) -> None:
    with pytest.raises(GraphDigestError) as raised:
        encode_value(value)

    assert str(raised.value) == "a graph element holds a value the digest cannot encode"


def test_a_list_of_floats_encodes_like_its_items_encoded_one_by_one() -> None:
    items = [0.1, -0.0, 0.0, 1.7976931348623157e308, 5e-324, 1 / 3]

    one_by_one = b"l" + len(items).to_bytes(8, "big") + b"".join(encode_value(i) for i in items)

    assert encode_value(items) == one_by_one


def test_a_list_of_floats_with_a_non_finite_item_is_refused() -> None:
    with pytest.raises(GraphDigestError):
        encode_value([0.1, float("inf")])
