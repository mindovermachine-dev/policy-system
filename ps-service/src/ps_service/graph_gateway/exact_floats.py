"""Exact reads of 64-bit floats from FalkorDB (issue #207, AC-RD-007).

FalkorDB stores a double exactly (a parameter travels as shortest round-trip text and compares
equal), but every reply prints a double with 15 significant digits: `0.30000000000000004` comes
back `0.3` and the largest double (`1.7976931348623157e308`) rounds up to `inf`. A plain read
therefore cannot tell two different stored values apart, which breaks the no-op filter and any
digest of a graph.

The remedy is server-side and lossless. Multiplying a double by a power of two is exact, so for a
non-zero float `v` the query asks for `s = 53 - floor(log2 |v|)` (an estimate that is at most one
binade off, which never matters) and `m = toInteger(v * 2^s)`: an integer of at most 2^55 that a
client turns back into `v` as `ldexp(float(m), -s)`, exactly. Zero needs no scale.

A property map bound to `p` is read as four columns (`PROPERTY_COLUMNS`), because the client's
cost of a reply is per value, not per byte: one 3,072-double embedding is 3,072 values, and a
lossy copy next to an exact one doubled the cost of every digest.

* `pairs`: `[[key, value], ...]` for every property except a list of floats (lossy for floats);
* `scalars`: `[[key, [s, text]], ...]` exact scalar floats (`text` is the integer's digits);
* `float lists`: `[[key, scales, integers], ...]` for each list whose items are all floats: two
  flat integer lists aligned with the items. A zero has integer `0` and scale `1` when it is a
  negative zero, `0` otherwise. This is the only copy of such a list, so the cost is two flat
  lists of integers (about 3 ms per embedding) instead of a nested pair per item (14 ms);
* `mixed lists`: the same shape for the other lists that hold a non-zero float (the gateway
  refuses mixed lists, but graphs written before it may hold them): the lossy list stays in
  `pairs` and the non-zero floats are patched in, `0` marking the positions to keep.

The list fragments iterate the items (`x IN p[k]`). Indexing a list held in a map (`p[k][i]`)
copies the whole list for every index, which made a 3,072-double embedding cost 145 ms (quadratic)
on a real FalkorDB. FalkorDB also evaluates both branches of a `CASE`, so arithmetic is only ever
applied to values that cannot make it fail.

`restore_properties` rebuilds the exact property map from the four columns.

Why not store another representation: the stored doubles are already exact, so no data changes.
Only the reading side needs this.
"""

from __future__ import annotations

import math
from typing import cast

from ps_service.graph_gateway.errors import UnexpectedGraphReplyError

_LN2 = "0.6931471805599453"
_SCALE = f"toInteger(53 - floor(log(abs({{v}})) / {_LN2}))"
_ENCODE = (
    f"head([s IN [{_SCALE}] | [s, toString({{v}} * 2.0 ^ toInteger(s / 2) "
    "* 2.0 ^ (s - toInteger(s / 2)))]])"
)
"""Cypher for `[s, exact integer text]` of the non-zero float expression `{v}`."""

EXACT_SCALAR_FLOATS = (
    "[k IN keys(p) WHERE typeOf(p[k]) = 'Float' AND p[k] <> 0 | [k, "
    + _ENCODE.replace("{v}", "p[k]")
    + "]]"
)
"""`[[key, [s, text]], ...]` for each non-zero scalar float property of the map `p`."""

_LIST_OR_EMPTY = "CASE WHEN typeOf(p[k]) = 'List' THEN p[k] ELSE [] END"
_IS_FLOAT_LIST = (
    f"(size({_LIST_OR_EMPTY}) > 0 AND size([x IN {_LIST_OR_EMPTY} WHERE typeOf(x) <> 'Float']) = 0)"
)
"""Whether `p[k]` is a non-empty list of floats (never fails: other values read as `[]`)."""

PROPERTY_PAIRS = f"[k IN keys(p) WHERE NOT {_IS_FLOAT_LIST} | [k, p[k]]]"
"""`[[key, value], ...]` for every property of `p` that is not a list of floats."""

_SCALE_OF_FLOAT = (
    "CASE WHEN x = 0 THEN (CASE WHEN 1.0 / x < 0 THEN 1 ELSE 0 END) "
    f"ELSE {_SCALE.replace('{v}', 'x')} END"
)
_INTEGER_OF_FLOAT = (
    f"CASE WHEN x = 0 THEN 0 ELSE head([s IN [{_SCALE.replace('{v}', 'x')}] | "
    "toInteger(x * 2.0 ^ toInteger(s / 2) * 2.0 ^ (s - toInteger(s / 2)))]) END"
)

FLOAT_LISTS = (
    f"[k IN keys(p) WHERE {_IS_FLOAT_LIST} | [k, "
    f"[x IN p[k] | {_SCALE_OF_FLOAT}], [x IN p[k] | {_INTEGER_OF_FLOAT}]]]"
)
"""`[[key, scales, integers], ...]` for each list of floats in `p`, aligned with its items."""

_IS_FLOAT = "typeOf(x) = 'Float' AND x <> 0"
_SAFE_VALUE = f"CASE WHEN {_IS_FLOAT} THEN x ELSE 1.0 END"
"""The item if it is a non-zero float, else `1.0`: arithmetic on it never fails."""
_MIXED_SCALE = (
    f"head([y IN [{_SAFE_VALUE}] | CASE WHEN {_IS_FLOAT} THEN {_SCALE.replace('{v}', 'y')} "
    "ELSE 0 END])"
)
_MIXED_INTEGER = (
    f"head([y IN [{_SAFE_VALUE}] | head([s IN [{_SCALE.replace('{v}', 'y')}] | "
    f"CASE WHEN {_IS_FLOAT} THEN toInteger(y * 2.0 ^ toInteger(s / 2) "
    "* 2.0 ^ (s - toInteger(s / 2))) ELSE 0 END])])"
)

MIXED_LISTS = (
    f"[k IN keys(p) WHERE NOT {_IS_FLOAT_LIST} "
    f"AND size([x IN {_LIST_OR_EMPTY} WHERE {_IS_FLOAT}]) > 0 | [k, "
    f"[x IN {_LIST_OR_EMPTY} | {_MIXED_SCALE}], [x IN {_LIST_OR_EMPTY} | {_MIXED_INTEGER}]]]"
)
"""`[[key, scales, integers], ...]` for each other list of `p` that holds a non-zero float."""

PROPERTY_COLUMNS = f"{PROPERTY_PAIRS}, {EXACT_SCALAR_FLOATS}, {FLOAT_LISTS}, {MIXED_LISTS}"
"""The four columns, for a query that binds the property map to `p` (`properties(n) AS p`)."""

PROPERTY_COLUMN_COUNT = 4

_PAIR_WIDTH = 2
_TRIPLE_WIDTH = 3
_REPLY_MESSAGE = "the graph answered an exact-float read with an unexpected shape"


def restore_properties(
    pairs: object, scalars: object, float_lists: object, mixed_lists: object
) -> dict[str, object]:
    """Return the exact property map the four columns of `PROPERTY_COLUMNS` describe.

    Raises:
        UnexpectedGraphReplyError: a column does not have the promised shape, or names a property
            the map does not hold. The message names nothing the graph returned.
    """
    restored = _from_pairs(pairs)
    for key, encoded in _pairs(scalars):
        restored[_float_key(restored, key)] = _decode(encoded)
    for key, scales, integers in _triples(float_lists):
        if not isinstance(key, str):
            raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
        restored[key] = _float_list(scales, integers)
    for key, scales, integers in _triples(mixed_lists):
        _patch_list(_list_property(restored, key), scales, integers)
    return restored


def _from_pairs(column: object) -> dict[str, object]:
    """Return `[[key, value], ...]` as a dict, or raise if it is anything else."""
    properties: dict[str, object] = {}
    for key, value in _pairs(column):
        if not isinstance(key, str):
            raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
        properties[key] = value
    return properties


def _float_list(scales: object, integers: object) -> list[object]:
    """Rebuild a list of floats from its aligned scales and integers (zero: its sign in scale)."""
    scale_list, integer_list = _aligned_integers(scales, integers)
    try:
        return [
            math.ldexp(float(integer), -scale) if integer else (-0.0 if scale == 1 else 0.0)
            for scale, integer in zip(scale_list, integer_list, strict=True)
        ]
    except (ValueError, OverflowError) as exc:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE) from exc


def _patch_list(items: list[object], scales: object, integers: object) -> None:
    """Replace, in place, the items that `integers` marks (non-zero) with their exact floats."""
    scale_list, integer_list = _aligned_integers(scales, integers)
    if len(scale_list) != len(items):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    try:
        for index, (scale, integer) in enumerate(zip(scale_list, integer_list, strict=True)):
            if integer:
                items[index] = math.ldexp(float(integer), -scale)
    except (ValueError, OverflowError) as exc:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE) from exc


def _aligned_integers(scales: object, integers: object) -> tuple[list[int], list[int]]:
    """Return both columns as equally long lists of integers (booleans are not), else raise."""
    if not isinstance(scales, list) or not isinstance(integers, list):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    scale_list = cast("list[int]", scales)
    integer_list = cast("list[int]", integers)
    if (
        len(scale_list) != len(integer_list)
        or not set(map(type, scale_list)) <= {int}
        or not set(map(type, integer_list)) <= {int}
    ):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return scale_list, integer_list


def _pairs(column: object) -> list[tuple[object, object]]:
    """Return a column as `(first, second)` pairs, or raise if it is anything else."""
    if not isinstance(column, list):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    pairs: list[tuple[object, object]] = []
    for item in cast("list[object]", column):  # narrowed to a list; its items are checked below
        if not isinstance(item, list) or len(cast("list[object]", item)) != _PAIR_WIDTH:
            raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
        first, second = cast("list[object]", item)
        pairs.append((first, second))
    return pairs


def _triples(column: object) -> list[tuple[object, object, object]]:
    """Return a column as `(key, scales, integers)` triples, or raise if it is anything else."""
    if not isinstance(column, list):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    triples: list[tuple[object, object, object]] = []
    for item in cast("list[object]", column):  # narrowed to a list; its items are checked below
        if not isinstance(item, list) or len(cast("list[object]", item)) != _TRIPLE_WIDTH:
            raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
        key, scales, integers = cast("list[object]", item)
        triples.append((key, scales, integers))
    return triples


def _decode(encoded: object) -> float:
    """Return the float that `[s, text]` stands for (`text` is an exact integer's digits)."""
    ((scale, text),) = _pairs([encoded])
    if not isinstance(scale, int) or not isinstance(text, str):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    try:
        return math.ldexp(float(text), -scale)
    except (ValueError, OverflowError) as exc:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE) from exc


def _float_key(properties: dict[str, object], key: object) -> str:
    """Return `key` once it names a float property of `properties`, else raise."""
    if not isinstance(key, str) or not isinstance(properties.get(key), float):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return key


def _list_property(properties: dict[str, object], key: object) -> list[object]:
    """Replace the list property `key` by a copy and return that copy, else raise."""
    value = properties.get(key) if isinstance(key, str) else None
    if not isinstance(key, str) or not isinstance(value, list):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    copied = list(cast("list[object]", value))
    properties[key] = copied
    return copied
