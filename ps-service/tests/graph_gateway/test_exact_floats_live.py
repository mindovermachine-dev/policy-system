"""`falkordb_live`: exact 64-bit float reads against a real FalkorDB (#207, AC-RD-007).

Proves the Cypher fragments of `exact_floats` give back every stored double bit-identically
where a plain read does not, and that the hermetic mirror in `_fakes` encodes the same values.
"""

from __future__ import annotations

import math
import random
import struct
import time
from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway._fakes import property_columns
from ps_service.graph_gateway.exact_floats import PROPERTY_COLUMNS, restore_properties

if TYPE_CHECKING:
    from graph_gateway.live_graphs import LiveGraphs

pytestmark = pytest.mark.falkordb_live

_HARD = (
    0.1,
    -0.0,
    1.7976931348623157e308,
    -1.7976931348623157e308,
    5e-324,
    -5e-324,
    2.2250738585072014e-308,
    0.30000000000000004,
    1 / 3,
    1e22,
    float(2**53),
    math.nextafter(1.0, 2.0),
    math.nextafter(2.0, 0.0),
)
_READ = (
    "CREATE (n:Probe {id: 'p', weight: $weight, embedding: $embedding, name: 'a', n: 1}) "
    f"WITH n, properties(n) AS p RETURN properties(n), {PROPERTY_COLUMNS}"
)


def _random_doubles(count: int) -> list[float]:
    generator = random.Random(207)  # noqa: S311 -- reproducible test data, not security
    values: list[float] = []
    while len(values) < count:
        (value,) = struct.unpack(">d", generator.getrandbits(64).to_bytes(8, "big"))
        if math.isfinite(value):
            values.append(value)
    return values


def test_a_plain_read_loses_bits_on_the_real_graph(live_graphs: LiveGraphs) -> None:
    graph = live_graphs.new()

    ((properties, *_),) = graph.rows(_READ, {"weight": 0.30000000000000004, "embedding": _HARD})

    stored = cast("dict[str, object]", properties)
    assert stored["weight"] != 0.30000000000000004
    assert math.isinf(cast("list[float]", stored["embedding"])[2])


def test_the_exact_columns_restore_every_double_bit_identically(live_graphs: LiveGraphs) -> None:
    graph = live_graphs.new()
    embedding = [*_HARD, *_random_doubles(2000)]

    ((_, *columns),) = graph.rows(_READ, {"weight": 0.30000000000000004, "embedding": embedding})

    restored = restore_properties(*columns)
    assert cast("float", restored["weight"]).hex() == (0.30000000000000004).hex()
    assert [v.hex() for v in cast("list[float]", restored["embedding"])] == [
        v.hex() for v in embedding
    ]
    assert restored["n"] == 1
    assert restored["name"] == "a"


def test_the_hermetic_mirror_encodes_the_same_values_as_the_real_graph(
    live_graphs: LiveGraphs,
) -> None:
    graph = live_graphs.new()
    embedding = [*_HARD, *_random_doubles(200)]
    properties: dict[str, object] = {
        "id": "p",
        "weight": 0.30000000000000004,
        "embedding": embedding,
        "n": 1,
        "name": "a",
    }

    ((_, *real),) = graph.rows(_READ, {"weight": 0.30000000000000004, "embedding": embedding})

    assert restore_properties(*real) == restore_properties(*property_columns(properties))


def test_a_list_that_mixes_floats_with_integers_comes_back_exact_and_typed(
    live_graphs: LiveGraphs,
) -> None:
    # The gateway refuses such a list; a graph written before it may hold one.
    graph = live_graphs.new()
    graph.rows("CREATE (:Probe {id: 'm', mixed: [1, 0.30000000000000004, 2]})")

    ((columns,),) = graph.rows(
        f"MATCH (n:Probe {{id: 'm'}}) WITH n, properties(n) AS p RETURN [{PROPERTY_COLUMNS}]"
    )

    restored = restore_properties(*cast("list[object]", columns))
    values = cast("list[object]", restored["mixed"])
    assert [type(v) for v in values] == [int, float, int]
    assert cast("float", values[1]).hex() == (0.30000000000000004).hex()


def test_negative_and_positive_zero_in_a_list_of_floats_keep_their_sign(
    live_graphs: LiveGraphs,
) -> None:
    graph = live_graphs.new()

    ((_, *columns),) = graph.rows(
        "CREATE (n:Probe {id: 'z', embedding: $embedding}) "
        f"WITH n, properties(n) AS p RETURN properties(n), {PROPERTY_COLUMNS}",
        {"embedding": [-0.0, 0.0, -0.0, 1.5]},
    )

    restored = cast("list[float]", restore_properties(*columns)["embedding"])
    assert [v.hex() for v in restored] == [v.hex() for v in (-0.0, 0.0, -0.0, 1.5)]


_EMBEDDING_LENGTH = 3072
_EMBEDDING_NODES = 20
_EXACT_LIST_BUDGET_SECONDS = 1.0
"""Linear cost is about 4 ms per node; the old quadratic form (`p[k][i]`) took 145 ms per node."""


def test_exact_list_columns_of_embedding_nodes_cost_time_linear_in_the_list_length(
    live_graphs: LiveGraphs,
) -> None:
    # `p[k][i]` copied the whole list `p[k]` for every index, so a 3,072-double embedding cost
    # 145 ms and a CRA x10 digest (5,220 embeddings) over 12 minutes. Found by the S19 load.
    graph = live_graphs.new()
    embedding = _random_doubles(_EMBEDDING_LENGTH)
    graph.rows(
        "UNWIND range(1, $count) AS i CREATE (:Probe {id: toString(i), embedding: $embedding})",
        {"count": _EMBEDDING_NODES, "embedding": embedding},
    )

    started = time.perf_counter()
    rows = graph.rows(f"MATCH (n:Probe) WITH n, properties(n) AS p RETURN {PROPERTY_COLUMNS}")
    seconds = time.perf_counter() - started

    assert len(rows) == _EMBEDDING_NODES
    assert seconds < _EXACT_LIST_BUDGET_SECONDS
