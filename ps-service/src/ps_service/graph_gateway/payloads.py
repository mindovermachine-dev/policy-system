"""Content-addressed payloads of the graph mutation log (issue #205).

Two kinds of value are too large or too exact for an inline column and live in
`graph_log.payloads`, addressed by the SHA-256 of their exact stored bytes:

- embeddings, always: IEEE-754 binary64 little-endian, one value after another, so a read-back
  is bit-identical (NaN payloads, signed zeros and subnormals included). 32-bit storage would
  change most real values, so it is never used;
- JSON content whose canonical encoding exceeds `INLINE_CONTENT_LIMIT_BYTES`: stored as
  canonical UTF-8 bytes, not `jsonb`, because `jsonb` reorders keys and normalises numbers.

Everything here is pure: the store decides when to write.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    """One payload row to store: its content address, kind and exact body."""

    payload_hash: str
    kind: str
    body: bytes


def payload_hash(body: bytes) -> str:
    """Return the content address of `body`: `sha256:` plus 64 lowercase hex characters."""
    return _HASH_PREFIX + hashlib.sha256(body).hexdigest()


def encode_embedding(values: Sequence[float]) -> bytes:
    """Encode `values` as little-endian binary64, whatever the host byte order.

    Raises:
        ValueError: `values` is empty.
    """
    if not values:
        msg = "an embedding must hold at least one value"
        raise ValueError(msg)
    return struct.pack(f"<{len(values)}d", *values)


def decode_embedding(body: bytes) -> tuple[float, ...]:
    """Decode a body written by `encode_embedding` back into the identical values.

    Raises:
        ValueError: `body` is not a whole, non-zero number of 8-byte values.
    """
    if not body or len(body) % _BYTES_PER_VALUE:
        msg = "an embedding body must be a whole, non-zero number of 8-byte values"
        raise ValueError(msg)
    return struct.unpack(f"<{len(body) // _BYTES_PER_VALUE}d", body)


def canonical_json_bytes(content: dict[str, object]) -> bytes:
    """Encode `content` as canonical UTF-8 JSON: sorted keys, no whitespace, no ASCII escaping.

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
    """Build the payload row for an embedding."""
    body = encode_embedding(values)
    return StoredPayload(payload_hash(body), PAYLOAD_KIND_EMBEDDING, body)


def json_payload_if_large(content: dict[str, object]) -> StoredPayload | None:
    """Build the payload row for `content`, or None when it is small enough to stay inline."""
    body = canonical_json_bytes(content)
    if len(body) <= INLINE_CONTENT_LIMIT_BYTES:
        return None
    return StoredPayload(payload_hash(body), PAYLOAD_KIND_JSON, body)


def decode_json_content(body: bytes) -> dict[str, object]:
    """Decode a `json` payload body back into the content it was written from."""
    decoded: dict[str, object] = json.loads(body.decode("utf-8"))
    return decoded
