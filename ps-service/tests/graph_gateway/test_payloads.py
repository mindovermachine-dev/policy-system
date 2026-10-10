"""Fast tests for graph log payload encoding (issue #205, slice 5; AC-BI-007, AC-BI-008).

Embeddings are lossless 64-bit binary: every comparison here is on bit patterns, never on `==`,
because `-0.0 == 0.0` and `nan != nan` would hide exactly the loss these tests must catch.
"""

from __future__ import annotations

import hashlib
import math
import struct
import sys

import pytest
from pydantic import ValidationError

from ps_service.graph_gateway.models import GraphLogEntryDraft
from ps_service.graph_gateway.payloads import (
    INLINE_CONTENT_LIMIT_BYTES,
    PAYLOAD_KIND_EMBEDDING,
    PAYLOAD_KIND_JSON,
    canonical_json_bytes,
    decode_embedding,
    embedding_payload,
    encode_embedding,
    json_payload_if_large,
    payload_hash,
)


def _from_bits(bits: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


def _bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


_QUIET_NAN_WITH_PAYLOAD = _from_bits(0x7FF8_0000_DEAD_BEEF)
_NEGATIVE_QUIET_NAN = _from_bits(0xFFF8_0000_0000_0001)
_SMALLEST_SUBNORMAL = _from_bits(1)
_LARGEST_SUBNORMAL = _from_bits(0x000F_FFFF_FFFF_FFFF)
# Values that binary32 cannot hold: 32-bit storage would change every one of them.
_NOT_REPRESENTABLE_IN_32_BIT = (0.1, 1 / 3, math.pi, 1e-300, 1e300, 1.0000000000000002)

_EDGE_VALUES = (
    0.0,
    -0.0,
    _SMALLEST_SUBNORMAL,
    _LARGEST_SUBNORMAL,
    sys.float_info.max,
    sys.float_info.min,
    sys.float_info.epsilon,
    math.inf,
    -math.inf,
    math.nan,
    _QUIET_NAN_WITH_PAYLOAD,
    _NEGATIVE_QUIET_NAN,
    *_NOT_REPRESENTABLE_IN_32_BIT,
)


def test_embedding_round_trips_bit_identically_for_edge_values() -> None:
    decoded = decode_embedding(encode_embedding(_EDGE_VALUES))

    assert [_bits(value) for value in decoded] == [_bits(value) for value in _EDGE_VALUES]


def test_embedding_edge_values_really_include_distinct_nan_bit_patterns() -> None:
    nan_patterns = {_bits(value) for value in _EDGE_VALUES if math.isnan(value)}

    assert len(nan_patterns) >= 3  # the test inputs would not catch a canonicalising NaN


def test_embedding_encoding_is_little_endian_regardless_of_host() -> None:
    encoded = encode_embedding((1.0, -2.5))

    assert encoded == bytes.fromhex("000000000000F03F") + bytes.fromhex("00000000000004C0")
    assert len(encoded) == 8 * 2


@pytest.mark.parametrize("value", _NOT_REPRESENTABLE_IN_32_BIT)
def test_embedding_of_float32_precision_loss_is_impossible(value: float) -> None:
    try:
        through_32_bit = struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:  # too large for binary32 at all: also a loss
        through_32_bit = math.inf
    assert _bits(through_32_bit) != _bits(value)  # the premise: 32-bit storage would lose it

    (decoded,) = decode_embedding(encode_embedding((value,)))

    assert _bits(decoded) == _bits(value)


def test_empty_embedding_is_rejected() -> None:
    with pytest.raises(ValueError, match="embedding"):
        encode_embedding(())


def test_decoding_a_body_that_is_not_a_whole_number_of_doubles_is_rejected() -> None:
    with pytest.raises(ValueError, match="embedding"):
        decode_embedding(b"\x00" * 9)


def test_payload_hash_is_prefixed_sha256_of_exact_bytes() -> None:
    body = b"\x00\x01exact bytes\xff"

    assert payload_hash(body) == "sha256:" + hashlib.sha256(body).hexdigest()


def test_canonical_json_is_key_order_independent() -> None:
    left = canonical_json_bytes({"b": 1, "a": {"y": [1, 2], "x": "é"}})
    right = canonical_json_bytes({"a": {"x": "é", "y": [1, 2]}, "b": 1})

    assert left == right
    assert left == '{"a":{"x":"é","y":[1,2]},"b":1}'.encode()


def test_non_finite_json_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="content"):
        canonical_json_bytes({"x": math.nan})


def _content_of_canonical_size(size: int) -> dict[str, object]:
    overhead = len(canonical_json_bytes({"k": ""}))
    return {"k": "x" * (size - overhead)}


def test_small_content_stays_inline_and_large_content_goes_to_payload() -> None:
    at_limit = _content_of_canonical_size(INLINE_CONTENT_LIMIT_BYTES)
    over_limit = _content_of_canonical_size(INLINE_CONTENT_LIMIT_BYTES + 1)
    assert len(canonical_json_bytes(at_limit)) == INLINE_CONTENT_LIMIT_BYTES
    assert len(canonical_json_bytes(over_limit)) == INLINE_CONTENT_LIMIT_BYTES + 1

    assert json_payload_if_large(at_limit) is None
    stored = json_payload_if_large(over_limit)
    assert stored is not None
    assert stored.kind == PAYLOAD_KIND_JSON
    assert stored.body == canonical_json_bytes(over_limit)
    assert stored.payload_hash == payload_hash(stored.body)


def test_identical_large_content_yields_the_same_payload_hash() -> None:
    first = json_payload_if_large({"k": "y" * 5000, "n": 1})
    second = json_payload_if_large({"n": 1, "k": "y" * 5000})

    assert first is not None
    assert second is not None
    assert first.payload_hash == second.payload_hash


def test_embedding_payload_is_binary64_kind_addressed_by_its_bytes() -> None:
    stored = embedding_payload((0.1, 1 / 3))

    assert stored.kind == PAYLOAD_KIND_EMBEDDING
    assert stored.body == encode_embedding((0.1, 1 / 3))
    assert stored.payload_hash == payload_hash(stored.body)


def test_entry_draft_keeps_embedding_bit_patterns() -> None:
    draft = GraphLogEntryDraft(
        name="Capability",
        identity="cap-1",
        content={},
        embedding=(-0.0, _QUIET_NAN_WITH_PAYLOAD, 0.1),
    )

    assert draft.embedding is not None
    assert [_bits(value) for value in draft.embedding] == [
        _bits(-0.0),
        _bits(_QUIET_NAN_WITH_PAYLOAD),
        _bits(0.1),
    ]


def test_entry_draft_rejects_an_empty_embedding() -> None:
    with pytest.raises(ValidationError):
        GraphLogEntryDraft(name="Capability", identity="cap-1", content={}, embedding=())


def test_entry_draft_without_embedding_defaults_to_none() -> None:
    assert GraphLogEntryDraft(name="Capability", identity="c", content={}).embedding is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param({"op": "upsert_node", "properties": {"offset": -0.0}}, id="negative-zero"),
        pytest.param({"op": "upsert_node", "properties": {"weight": 1.0}}, id="float-one"),
        pytest.param({"op": "upsert_node", "properties": {"big": 1e22}}, id="beyond-int64"),
        pytest.param({"op": "upsert_node", "properties": {"series": [0.1, 2]}}, id="in-a-list"),
    ],
)
def test_content_containing_a_float_is_stored_as_a_json_payload(
    content: dict[str, object],
) -> None:
    # `jsonb` is a `numeric`: it has no negative zero, turns 1.0 into 1 and 1e22 into a long
    # integer. Only the exact UTF-8 text keeps a float what it was (live proof: #207 S14L).
    payload = json_payload_if_large(content)

    assert payload is not None
    assert payload.kind == PAYLOAD_KIND_JSON


def test_content_without_a_float_stays_inline_when_small() -> None:
    assert (
        json_payload_if_large({"op": "upsert_node", "properties": {"n": 2**53, "ok": True}}) is None
    )
