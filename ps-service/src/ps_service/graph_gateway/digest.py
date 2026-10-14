"""The canonical digest of one FalkorDB graph.

The digest depends on logical content only: ids, properties (embeddings included), labels and
relationships. It does not depend on FalkorDB's internal ids or on
insertion order. Every element is encoded as a type-tagged, length-prefixed byte string, hashed
with SHA-256, and the sorted 32-byte hashes are hashed again under a versioned domain prefix.

Memory bound: a scan is chunked by internal-id cursor (the cursor is never hashed) and each
element is hashed and dropped, so only 32 bytes per element are retained, plus one chunk of rows
in flight (50 nodes by default; with 3072-double embeddings about 5 MB of Python floats). The
digest holds the graph's lock for its duration when a checkpoint is requested, because it reads
the whole graph.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

from ps_service.graph_gateway.cypher import DIGEST_EDGE_SCAN, DIGEST_NODE_SCAN
from ps_service.graph_gateway.errors import GraphDigestError, UnexpectedGraphReplyError
from ps_service.graph_gateway.exact_floats import restore_properties

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

DEFAULT_NODE_CHUNK_ROWS = 50
"""A row with a 3,072-double embedding costs about 4 ms of server time; 50 rows stay far below
FalkorDB's default 1000 ms query timeout (250 rows did not)."""
DEFAULT_EDGE_CHUNK_ROWS = 5000

_DOMAIN = b"ps-graph-digest-v1"
_ALGORITHM = "sha256"
_NODE_ROW_WIDTH = 6
_EDGE_ROW_WIDTH = 10
_REPLY_MESSAGE = "the graph answered a digest scan with an unexpected shape"
_VALUE_MESSAGE = "a graph element holds a value the digest cannot encode"
_LENGTH_BYTES = 8
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class _ScanResult(Protocol):
    """The rows of a scan; both the writable and the read-only handle's results have them."""

    @property
    def result_set(self) -> list[object]:
        """The rows exactly as the driver returned them."""
        ...


class QueryableGraph(Protocol):
    """What a digest needs of a graph: it can run a read query.

    A writable handle and a read-only one both satisfy it, so the checkpoint path (writable
    opener, under the graph's lock) and the drift check (read-only opener, no lock) share the
    one digest. A graph key that does not exist reads as the empty graph.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> _ScanResult:
        """Run Cypher `q` with `params` and return its rows.

        Args:
            q: Read-only Cypher text.
            params: Query parameters, or `None` for none.

        Returns:
            The query result, whose `result_set` holds the rows.
        """
        ...


@dataclass(frozen=True)
class DigestSettings:
    """Tunables of the digest scans.

    Attributes:
        node_chunk_rows: Nodes read per scan query; at least 1.
        edge_chunk_rows: Relationships read per scan query; at least 1.

    Raises:
        ValueError: Either chunk size is below 1 (checked on construction).
    """

    node_chunk_rows: int = DEFAULT_NODE_CHUNK_ROWS
    """Nodes read per scan query."""
    edge_chunk_rows: int = DEFAULT_EDGE_CHUNK_ROWS
    """Relationships read per scan query."""

    def __post_init__(self) -> None:
        """Reject a chunk size that would make the scan loop forever or read nothing."""
        if self.node_chunk_rows < 1 or self.edge_chunk_rows < 1:
            message = "digest chunk sizes must be at least 1"
            raise ValueError(message)


def canonical_digest(graph: QueryableGraph, settings: DigestSettings | None = None) -> str:
    """Return `sha256:<hex>` of `graph`'s logical content.

    Args:
        graph: The graph to digest; a missing graph key reads as the empty graph.
        settings: Scan chunk sizes; `None` uses the defaults.

    Returns:
        `sha256:` followed by 64 hex digits. An empty graph yields `EMPTY_GRAPH_DIGEST`.

    Raises:
        GraphDigestError: a scan answered an unexpected shape, or an element holds a value the
            gateway cannot have written.

    Side Effects:
        Runs read queries against `graph`; it writes nothing.
    """
    chosen = settings if settings is not None else DigestSettings()
    return combine_element_hashes(
        chain(
            _scan_hashes(graph, DIGEST_NODE_SCAN, chosen.node_chunk_rows, _node_element),
            _scan_hashes(graph, DIGEST_EDGE_SCAN, chosen.edge_chunk_rows, _edge_element),
        )
    )


class ElementHashes(NamedTuple):
    """The 32-byte hash of every node and of every relationship of a graph.

    Attributes:
        nodes: One hash per node, in scan order.
        edges: One hash per relationship, in scan order.
    """

    nodes: tuple[bytes, ...]
    edges: tuple[bytes, ...]


def element_hashes(graph: QueryableGraph, settings: DigestSettings | None = None) -> ElementHashes:
    """Return the element hashes `canonical_digest` combines, split by kind.

    Lets a caller tell which elements two graphs disagree on without reading any content (the
    hashes are one-way). Memory: 32 bytes per element, as for the digest itself.

    Args:
        graph: The graph to scan; a missing graph key reads as the empty graph.
        settings: Scan chunk sizes; `None` uses the defaults.

    Returns:
        The node hashes and the relationship hashes; both empty for an empty graph.

    Raises:
        GraphDigestError: a scan answered an unexpected shape, or an element holds a value the
            gateway cannot have written.

    Side Effects:
        Runs read queries against `graph`; it writes nothing.
    """
    chosen = settings if settings is not None else DigestSettings()
    return ElementHashes(
        nodes=tuple(_scan_hashes(graph, DIGEST_NODE_SCAN, chosen.node_chunk_rows, _node_element)),
        edges=tuple(_scan_hashes(graph, DIGEST_EDGE_SCAN, chosen.edge_chunk_rows, _edge_element)),
    )


def combine_element_hashes(hashes: Iterable[bytes]) -> str:
    """Return the digest of a graph from the 32-byte hash of each of its elements.

    The hashes are sorted before they are hashed again, so the order the elements were read in
    (and so the insertion order and FalkorDB's internal ids) cannot matter, while a repeated
    element still counts twice.

    Args:
        hashes: The 32-byte hash of each element; may be empty.

    Returns:
        `sha256:` followed by 64 hex digits.
    """
    combined = hashlib.sha256(_DOMAIN + b"".join(sorted(hashes))).hexdigest()
    return f"{_ALGORITHM}:{combined}"


def _scan_hashes(
    graph: QueryableGraph,
    query: str,
    chunk_rows: int,
    encode: Callable[[object], tuple[int, bytes]],
) -> Iterator[bytes]:
    """Yield the hash of each element a scan returns, chunk by chunk, along the internal-id cursor.

    `encode` turns a row into its internal id (the cursor, never hashed) and its encoded element.
    """
    after = -1
    while True:
        rows = graph.query(query, {"after": after, "limit": chunk_rows}).result_set
        for row in rows:
            cursor, element = encode(row)
            after = max(after, cursor)
            yield hashlib.sha256(element).digest()
        if len(rows) < chunk_rows:
            return


def _node_element(row: object) -> tuple[int, bytes]:
    """Return a node row's internal-id cursor and its encoded element."""
    if not isinstance(row, list) or len(cast("list[object]", row)) != _NODE_ROW_WIDTH:
        raise GraphDigestError(_REPLY_MESSAGE)
    cursor, labels, *columns = cast("list[object]", row)
    if not isinstance(cursor, int):
        raise GraphDigestError(_REPLY_MESSAGE)
    exact = _restored(columns)
    return cursor, b"N" + _label_set(labels) + encode_properties(exact)


def _edge_element(row: object) -> tuple[int, bytes]:
    """Return a relationship row's internal-id cursor and its encoded element."""
    if not isinstance(row, list) or len(cast("list[object]", row)) != _EDGE_ROW_WIDTH:
        raise GraphDigestError(_REPLY_MESSAGE)
    cursor, kind, source_labels, source_id, target_labels, target_id, *columns = cast(
        "list[object]", row
    )
    if not isinstance(cursor, int) or not isinstance(kind, str):
        raise GraphDigestError(_REPLY_MESSAGE)
    exact = _restored(columns)
    endpoints = _endpoint(source_labels, source_id) + _endpoint(target_labels, target_id)
    return cursor, b"E" + _text(kind) + endpoints + encode_properties(exact)


def _restored(columns: list[object]) -> dict[str, object]:
    """Return the exact property map of a scan row from its four property columns."""
    try:
        return restore_properties(*columns)
    except UnexpectedGraphReplyError as exc:
        raise GraphDigestError(_REPLY_MESSAGE) from exc


def _endpoint(labels: object, node_id: object) -> bytes:
    """Encode the end of a relationship: the label set and the logical id of the node."""
    if not isinstance(node_id, str):
        raise GraphDigestError(_REPLY_MESSAGE)
    return _label_set(labels) + _text(node_id)


def _label_set(labels: object) -> bytes:
    """Encode a node's label set: the sorted names, so the order FalkorDB lists them in is moot."""
    if not isinstance(labels, list):
        raise GraphDigestError(_REPLY_MESSAGE)
    names = cast("list[object]", labels)
    if not all(isinstance(name, str) for name in names):
        raise GraphDigestError(_REPLY_MESSAGE)
    ordered = sorted(cast("list[str]", names))
    return _length(len(ordered)) + b"".join(_text(name) for name in ordered)


def encode_properties(properties: dict[str, object]) -> bytes:
    """Encode a property map: its size, then each key and value in key order.

    Args:
        properties: Property names to values; each value must satisfy `encode_value`.

    Returns:
        The unambiguous byte encoding; key order in `properties` does not matter.

    Raises:
        GraphDigestError: A value cannot be encoded.
    """
    return _length(len(properties)) + b"".join(
        _text(key) + encode_value(properties[key]) for key in sorted(properties)
    )


def encode_value(value: object) -> bytes:
    """Encode one property value as a type tag followed by an unambiguous body.

    The tag keeps `True`, `1` and `1.0` apart; floats are their 8 IEEE-754 bytes, so `-0.0`
    differs from `0.0` and one unit in the last place shows. A value the gateway cannot have
    written (None, a map, a non-finite float, an integer beyond 64 bits) is refused.

    Args:
        value: A str, bool, int64, finite float, or list of those.

    Returns:
        The type tag followed by the encoded body.

    Raises:
        GraphDigestError: `value` is not a str, bool, int64, finite float or list of those.
    """
    match value:
        case bool():  # `bool` is an `int`, so it comes first and never reaches the int case
            return _encode_bool(value)
        case int():
            return _encode_int(value)
        case float():
            return _encode_float(value)
        case str():
            return b"s" + _text(value)
        case list():
            return _encode_list(cast("list[object]", value))
        case _:
            raise GraphDigestError(_VALUE_MESSAGE)


def _text(text: str) -> bytes:
    """Encode `text` as its length and UTF-8 bytes."""
    raw = text.encode("utf-8")
    return _length(len(raw)) + raw


def _encode_bool(value: bool) -> bytes:  # noqa: FBT001 -- a private encoder, called only by type
    """Encode a bool as a tag and one byte."""
    return b"b" + (b"\x01" if value else b"\x00")


def _encode_int(value: int) -> bytes:
    """Encode an int64 as a tag and eight signed big-endian bytes."""
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise GraphDigestError(_VALUE_MESSAGE)
    return b"i" + value.to_bytes(_LENGTH_BYTES, "big", signed=True)


def _encode_float(value: float) -> bytes:
    """Encode a finite float as a tag and its eight IEEE-754 bytes."""
    if not math.isfinite(value):
        raise GraphDigestError(_VALUE_MESSAGE)
    return b"f" + struct.pack(">d", value)


def _encode_list(items: list[object]) -> bytes:
    """Encode a list as a tag, its length and each encoded item."""
    if items and set(map(type, items)) == {float}:
        return b"l" + _length(len(items)) + _encode_floats(cast("list[float]", items))
    return b"l" + _length(len(items)) + b"".join(encode_value(item) for item in items)


def _encode_floats(items: list[float]) -> bytes:
    """Encode a list of floats as `encode_value` would each one, in one call (a long embedding)."""
    if not all(map(math.isfinite, items)):
        raise GraphDigestError(_VALUE_MESSAGE)
    arguments: list[object] = [b"f"] * (2 * len(items))
    arguments[1::2] = items
    return struct.pack(">" + "cd" * len(items), *arguments)


def _length(size: int) -> bytes:
    """Encode a size as eight big-endian bytes."""
    return size.to_bytes(_LENGTH_BYTES, "big")


EMPTY_GRAPH_DIGEST = combine_element_hashes(())
"""The digest of a graph with no node and no relationship (a log that nets to empty)."""
