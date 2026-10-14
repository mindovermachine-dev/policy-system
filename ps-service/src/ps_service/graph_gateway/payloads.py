"""Content-addressed payloads of the graph mutation log.

Two kinds of value are too large or too exact for an inline column and live in
`graph_log.payloads`, addressed by the SHA-256 of their exact stored bytes:

- embeddings, always: IEEE-754 binary64 little-endian, one value after another, so a read-back
  is bit-identical (NaN payloads, signed zeros and subnormals included). 32-bit storage would
  change most real values, so it is never used;
- JSON content whose canonical encoding exceeds `INLINE_CONTENT_LIMIT_BYTES`, and any content
  that holds a float anywhere: stored as canonical UTF-8 bytes, not `jsonb`, because `jsonb`
  reorders keys and normalises numbers (it is a `numeric`: no negative zero, `1.0` comes back as
  `1`, `1e22` as a 23-digit integer), which would change what a replay rebuilds.

Everything here is pure: the store decides when to write.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

PAYLOAD_KIND_EMBEDDING = "float64le"
"""`graph_log.payloads.kind` of an embedding: little-endian binary64 values."""
PAYLOAD_KIND_JSON = "json"
"""`graph_log.payloads.kind` of large content: canonical UTF-8 JSON."""
INLINE_CONTENT_LIMIT_BYTES = 2048
"""Content up to this many canonical bytes stays inline; larger content becomes a payload."""

_HASH_PREFIX = "sha256:"
_BYTES_PER_VALUE = 8
_VALUE_FORMAT = "<d"


@dataclass(frozen=True)
class StoredPayload:
    """One payload row to store: its content address, kind and exact body.

    Attributes:
        payload_hash: Content address, `sha256:` plus 64 lowercase hex characters of `body`.
        kind: `PAYLOAD_KIND_EMBEDDING` or `PAYLOAD_KIND_JSON`.
        body: The exact stored bytes; never re-encoded.
    """

    payload_hash: str
    kind: str
    body: bytes


def payload_hash(body: bytes) -> str:
    """Compute the content address under which a payload body is stored.

    Args:
        body: The exact bytes to be stored.

    Returns:
        `sha256:` plus the 64 lowercase hex characters of the SHA-256 of `body`.
    """
    return _HASH_PREFIX + hashlib.sha256(body).hexdigest()


def encode_embedding(values: Sequence[float]) -> bytes:
    """Encode an embedding as little-endian binary64, whatever the host byte order.

    Args:
        values: The embedding components; at least one.

    Returns:
        Eight bytes per value, one value after another; bit-identical on decode.

    Raises:
        ValueError: `values` is empty.
    """
    if not values:
        msg = "an embedding must hold at least one value"
        raise ValueError(msg)
    return struct.pack(f"<{len(values)}d", *values)


def decode_embedding(body: bytes) -> tuple[float, ...]:
    """Decode a body written by `encode_embedding` back into the identical values.

    Args:
        body: Little-endian binary64 bytes.

    Returns:
        The embedding components, bit-identical to those encoded.

    Raises:
        ValueError: `body` is not a whole, non-zero number of 8-byte values.
    """
    if not body or len(body) % _BYTES_PER_VALUE:
        msg = "an embedding body must be a whole, non-zero number of 8-byte values"
        raise ValueError(msg)
    return struct.unpack(f"<{len(body) // _BYTES_PER_VALUE}d", body)


def canonical_json_bytes(content: dict[str, object]) -> bytes:
    """Encode content as canonical UTF-8 JSON: sorted keys, no whitespace, no ASCII escaping.

    The encoding is deterministic, so equal content always yields equal bytes and equal hashes.

    Args:
        content: A JSON-representable mapping.

    Returns:
        The canonical UTF-8 bytes.

    Raises:
        ValueError: `content` holds a value JSON cannot carry (NaN, infinity, a non-JSON type).
    """
    try:
        return json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        msg = "content is not representable as canonical JSON"
        raise ValueError(msg) from exc


def embedding_payload(values: Sequence[float]) -> StoredPayload:
    """Build the payload row for an embedding.

    Args:
        values: The embedding components; at least one.

    Returns:
        A `StoredPayload` of kind `PAYLOAD_KIND_EMBEDDING` addressed by the hash of its bytes.

    Raises:
        ValueError: `values` is empty.
    """
    body = encode_embedding(values)
    return StoredPayload(payload_hash(body), PAYLOAD_KIND_EMBEDDING, body)


def json_payload_if_large(content: dict[str, object]) -> StoredPayload | None:
    """Build the payload row for content too large or too exact for an inline column.

    Content stays inline only when it is small and holds no float: the `jsonb` column does not
    keep a float exactly (see the module docstring).

    Args:
        content: A JSON-representable mapping.

    Returns:
        A `StoredPayload` of kind `PAYLOAD_KIND_JSON`, or None when the content stays inline in
        `jsonb`.

    Raises:
        ValueError: `content` is not representable as canonical JSON.
    """
    body = canonical_json_bytes(content)
    if len(body) <= INLINE_CONTENT_LIMIT_BYTES and not _holds_a_float(content):
        return None
    return StoredPayload(payload_hash(body), PAYLOAD_KIND_JSON, body)


def _holds_a_float(value: object) -> bool:
    """Whether `value` is, or contains at any depth, a float."""
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_holds_a_float(item) for item in cast("dict[str, object]", value).values())
    if isinstance(value, list | tuple):
        return any(_holds_a_float(item) for item in cast("list[object]", value))
    return False


def decode_json_content(body: bytes) -> dict[str, object]:
    """Decode a `json` payload body back into the content it was written from.

    Args:
        body: Canonical UTF-8 JSON bytes of a payload of kind `PAYLOAD_KIND_JSON`.

    Returns:
        The decoded mapping.

    Raises:
        ValueError: `body` is not valid UTF-8 JSON (including `json.JSONDecodeError` and
            `UnicodeDecodeError`, both `ValueError` subclasses).
    """
    decoded: dict[str, object] = json.loads(body.decode("utf-8"))
    return decoded
