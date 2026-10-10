"""Exact 64-bit float reads from FalkorDB (issue #207, AC-RD-007).

FalkorDB stores doubles exactly but answers a query with at most 15 significant digits, so a plain
read loses bits (`0.30000000000000004` comes back `0.3`, the largest double comes back `inf`).
The gateway therefore asks for every float a second time as an exact integer scaled by a power of
two, and `restore_properties` rebuilds the exact property map from the columns of a reply.
"""

from __future__ import annotations

import math
from typing import cast

import pytest

from graph_gateway._fakes import property_columns
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.exact_floats import (
    EXACT_SCALAR_FLOATS,
    FLOAT_LISTS,
    MIXED_LISTS,
    PROPERTY_PAIRS,
    restore_properties,
)

_HARD_DOUBLES = (
    0.1,
    -0.0,
    0.0,
    1.7976931348623157e308,
    -1.7976931348623157e308,
    5e-324,
    2.2250738585072014e-308,
    0.30000000000000004,
    1 / 3,
    1e22,
    float(2**53),
    math.nextafter(1.0, 2.0),
    math.nextafter(2.0, 0.0),
)


def _lossy(value: float) -> float:
    """What FalkorDB's reply carries: the value printed with 15 significant digits."""
    return float(f"{value:.15g}") if abs(value) < 1.79e308 else math.inf


def _lossy_value(value: object) -> object:
    if isinstance(value, float):
        return _lossy(value)
    if isinstance(value, list):
        return [_lossy_value(item) for item in cast("list[object]", value)]
    return value


def _restored(properties: dict[str, object]) -> dict[str, object]:
    return restore_properties(*property_columns(properties))


def test_the_lossy_reply_really_loses_bits_for_the_hard_doubles() -> None:
    assert _lossy(0.30000000000000004) != 0.30000000000000004
    assert _lossy(1.7976931348623157e308) == math.inf


@pytest.mark.parametrize("value", _HARD_DOUBLES)
def test_a_scalar_float_is_restored_bit_identically(value: float) -> None:
    restored = _restored({"weight": value})

    assert isinstance(restored["weight"], float)
    assert restored["weight"].hex() == value.hex()


def test_every_element_of_a_float_list_is_restored_bit_identically() -> None:
    restored = _restored({"embedding": list(_HARD_DOUBLES)})

    embedding = restored["embedding"]
    assert isinstance(embedding, list)
    assert [float(cast("float", v)).hex() for v in cast("list[object]", embedding)] == [
        v.hex() for v in _HARD_DOUBLES
    ]


def test_values_that_are_not_floats_are_left_as_they_are() -> None:
    properties: dict[str, object] = {
        "name": "a",
        "n": 3,
        "flag": True,
        "tags": ["x", "y"],
        "counts": [1, 2],
        "weight": 0.30000000000000004,
    }

    restored = _restored(properties)

    assert restored == properties
    assert type(restored["n"]) is int
    assert restored["flag"] is True


def test_a_map_without_floats_asks_for_nothing_to_restore() -> None:
    pairs, scalars, float_lists, mixed_lists = property_columns(
        {"name": "a", "n": 1, "tags": ["x"]}
    )

    assert (scalars, float_lists, mixed_lists) == ([], [], [])
    assert pairs == [["name", "a"], ["n", 1], ["tags", ["x"]]]


def test_a_list_of_floats_travels_only_as_exact_integers() -> None:
    pairs, _, float_lists, _ = property_columns({"embedding": [0.1, -0.0, 0.0], "name": "a"})

    assert pairs == [["name", "a"]]  # no lossy copy of the list
    ((key, scales, integers),) = cast("list[list[object]]", float_lists)
    assert key == "embedding"
    assert cast("list[int]", integers)[1:] == [0, 0]
    assert cast("list[int]", scales)[1:] == [1, 0]  # negative zero is flagged in the scales


def test_negative_zero_and_zero_in_a_float_list_are_restored_with_their_sign() -> None:
    restored = _restored({"embedding": [-0.0, 0.0, 1.5]})

    assert [
        math.copysign(1.0, cast("float", v)) for v in cast("list[object]", restored["embedding"])
    ] == [-1.0, 1.0, 1.0]


def test_a_list_that_mixes_floats_with_other_numbers_keeps_types_and_bits() -> None:
    restored = _restored({"mixed": [1, 0.30000000000000004, 2]})

    values = cast("list[object]", restored["mixed"])
    assert [type(v) for v in values] == [int, float, int]
    assert cast("float", values[1]).hex() == (0.30000000000000004).hex()


def test_a_malformed_exact_column_is_an_unexpected_reply_without_the_value() -> None:
    with pytest.raises(UnexpectedGraphReplyError) as raised:
        restore_properties([], [["weight", "not-a-pair"]], [], [])

    assert "not-a-pair" not in str(raised.value)


@pytest.mark.parametrize(
    "float_lists",
    [
        pytest.param([["embedding", [0, 0], [0]]], id="scales-and-integers-differ-in-length"),
        pytest.param([["embedding", ["x"], [1]]], id="scale-is-not-an-integer"),
        pytest.param([["embedding", [0], [1.5]]], id="integer-is-not-an-integer"),
        pytest.param([["embedding", [True], [1]]], id="scale-is-a-boolean"),
        pytest.param([["embedding", [0]]], id="a-column-is-missing"),
        pytest.param("not-a-list", id="the-column-is-not-a-list"),
    ],
)
def test_a_malformed_float_list_column_is_an_unexpected_reply(float_lists: object) -> None:
    with pytest.raises(UnexpectedGraphReplyError):
        restore_properties([], [], float_lists, [])


@pytest.mark.parametrize(
    "mixed_lists",
    [
        pytest.param([["mixed", [0, 0], [0]]], id="scales-and-integers-differ-in-length"),
        pytest.param([["mixed", [0], [0]]], id="shorter-than-the-list"),
        pytest.param([["missing", [0], [0]]], id="names-a-property-the-pairs-lack"),
    ],
)
def test_a_malformed_mixed_list_column_is_an_unexpected_reply(mixed_lists: object) -> None:
    with pytest.raises(UnexpectedGraphReplyError):
        restore_properties([["mixed", [1, 0.5]]], [], [], mixed_lists)


@pytest.mark.parametrize(
    "pairs",
    [
        pytest.param([["name"]], id="a-pair-of-one"),
        pytest.param([[1, "a"]], id="the-key-is-not-text"),
        pytest.param("not-a-list", id="the-column-is-not-a-list"),
    ],
)
def test_malformed_property_pairs_are_an_unexpected_reply(pairs: object) -> None:
    with pytest.raises(UnexpectedGraphReplyError):
        restore_properties(pairs, [], [], [])


def test_a_missing_exact_property_is_an_unexpected_reply() -> None:
    with pytest.raises(UnexpectedGraphReplyError):
        restore_properties([], [["weight", [0, "1.000000"]]], [], [])


def test_the_cypher_fragments_take_no_parameters_and_leave_no_placeholder_behind() -> None:
    for fragment in (EXACT_SCALAR_FLOATS, FLOAT_LISTS, MIXED_LISTS, PROPERTY_PAIRS):
        assert "$" not in fragment
        assert "typeOf" in fragment
        assert "{v}" not in fragment
