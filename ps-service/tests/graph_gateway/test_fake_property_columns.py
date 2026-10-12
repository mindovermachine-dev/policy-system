"""The in-memory FalkorDB fake's property columns are what its readable first version produced.

`property_columns` is hot in the equivalence suites (a digest scan re-encodes every embedding of
a 3,072-double list per scan), so it is memoised. This pins the encoding to a frozen, plain
reference so the speed-up cannot change what the fake answers (issue #214, slice 9).
"""

from __future__ import annotations

import math
from typing import cast

import pytest

from graph_gateway._fakes import lossy_reply, property_columns


def _nonzero(value: object) -> bool:
    return isinstance(value, float) and value != 0.0


def _float_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    items = cast("list[object]", value)
    return bool(items) and all(isinstance(i, float) for i in items)


def _aligned(items: list[object]) -> list[object]:
    scales: list[int] = []
    integers: list[int] = []
    for item in items:
        if _nonzero(item):
            scale = 54 - math.frexp(cast("float", item))[1]
            scales.append(scale)
            integers.append(int(math.ldexp(cast("float", item), scale)))
        else:
            negative = isinstance(item, float) and math.copysign(1.0, item) < 0
            scales.append(1 if negative else 0)
            integers.append(0)
    return [scales, integers]


def _reference(properties: dict[str, object]) -> list[object]:
    pairs = [[k, lossy_reply(v)] for k, v in properties.items() if not _float_list(v)]
    scalars = [
        [
            k,
            [
                54 - math.frexp(cast("float", v))[1],
                f"{math.ldexp(cast('float', v), 54 - math.frexp(cast('float', v))[1]):.6f}",
            ],
        ]
        for k, v in properties.items()
        if _nonzero(v)
    ]
    float_lists = [
        [k, *_aligned(cast("list[object]", v))] for k, v in properties.items() if _float_list(v)
    ]
    mixed = [
        [k, *_aligned(cast("list[object]", v))]
        for k, v in properties.items()
        if isinstance(v, list)
        and not _float_list(cast("object", v))
        and any(_nonzero(i) for i in cast("list[object]", v))
    ]
    return [pairs, scalars, float_lists, mixed]


_CASES: list[dict[str, object]] = [
    {},
    {"name": "x", "n": 3, "flag": True, "none": None},
    {"score": 0.1, "zero": 0.0, "neg_zero": -0.0, "big": 1.5e300, "tiny": -2.5e-300},
    {"embedding": [0.1, -0.25, 0.0, -0.0, 1e-9, 3.0]},
    {"mixed": [1, 0.5, "a"], "empty": [], "ints": [1, 2, 3]},
    {"embedding": [0.3, 0.7], "again": [0.3, 0.7]},
]


@pytest.mark.parametrize("properties", _CASES)
def test_property_columns_match_the_plain_reference_encoding(properties: dict[str, object]) -> None:
    assert property_columns(properties) == _reference(properties)


def test_a_mutated_list_is_re_encoded_not_served_from_a_stale_memo() -> None:
    embedding = [0.1, 0.2, 0.3]
    before = property_columns({"embedding": embedding})

    embedding[1] = 0.9
    after = property_columns({"embedding": embedding})

    assert before != after
    assert after == _reference({"embedding": embedding})


def test_a_zero_flipped_to_negative_zero_in_place_is_re_encoded() -> None:
    embedding = [0.5, 0.0, 0.25]
    property_columns({"embedding": embedding})

    embedding[1] = -0.0

    assert property_columns({"embedding": embedding}) == _reference({"embedding": embedding})
