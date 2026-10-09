"""`postgres_live` tests for payloads of the graph log (issue #205, slice 5).

Covers AC-BI-007 (a 64-bit embedding is stored as binary and read back bit-identical) and
AC-BI-008 (an identical large payload appended twice is stored once, both entries share its
hash). Runs the store as the real `ps_state` role against a scratch PS Postgres.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import math
import struct
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Protocol

import pytest

from ps_service.graph_gateway.errors import GraphLogPayloadError, GraphLogPersistenceError
from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.payloads import (
    INLINE_CONTENT_LIMIT_BYTES,
    PAYLOAD_KIND_EMBEDDING,
    PAYLOAD_KIND_JSON,
    canonical_json_bytes,
    payload_hash,
)
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

    from ps_service.logging import LogEmitter

pytestmark = pytest.mark.postgres_live

_CONCURRENT_APPENDERS = 6
_EMBEDDING_BYTES_PER_VALUE = 8


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


def _bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def _from_bits(bits: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


# Every value below changes when squeezed through binary32 (or is a distinct special pattern).
_EMBEDDING = (
    0.1,
    1 / 3,
    math.pi,
    -0.0,
    0.0,
    _from_bits(1),
    _from_bits(0x000F_FFFF_FFFF_FFFF),
    1e-300,
    1e300,
    math.inf,
    -math.inf,
    math.nan,
    _from_bits(0x7FF8_0000_DEAD_BEEF),
    _from_bits(0xFFF8_0000_0000_0001),
)


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _store(prov: Provisioned, emitter: LogEmitter | None = None) -> PsycopgGraphLogStore:
    return PsycopgGraphLogStore(prov.state_config(), emitter=emitter)


def _large_content(marker: str = "large") -> dict[str, object]:
    return {"title": marker, "body": "x" * (INLINE_CONTENT_LIMIT_BYTES + 100)}


def _group(graph: str, *entries: GraphLogEntryDraft) -> GraphLogGroupDraft:
    return GraphLogGroupDraft(graph=graph, entries=entries)


def _entry(
    identity: str, content: dict[str, object], embedding: tuple[float, ...] | None = None
) -> GraphLogEntryDraft:
    return GraphLogEntryDraft(
        name="Capability", identity=identity, content=content, embedding=embedding
    )


def _payload_rows(prov: Provisioned, *hashes: str) -> list[tuple[str, str, int, bytes]]:
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT payload_hash, kind, byte_length, body FROM graph_log.payloads "
            "WHERE payload_hash = ANY(%s) ORDER BY payload_hash",
            (list(hashes),),
        ).fetchall()
    return [(str(h), str(k), int(n), bytes(b)) for h, k, n, b in rows]  # pyright: ignore[reportArgumentType]  # psycopg row cells are untyped


def _entry_hashes(prov: Provisioned, graph: str) -> list[tuple[str | None, str | None]]:
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(
            "SELECT content_payload_hash, embedding_payload_hash FROM graph_log.entries "
            "WHERE graph = %s ORDER BY position",
            (graph,),
        ).fetchall()
    return [(content_hash, embedding_hash) for content_hash, embedding_hash in rows]  # pyright: ignore[reportReturnType]  # psycopg row cells are untyped


def test_embedding_is_stored_as_binary_and_read_back_bit_identical(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    store.append_group_standalone(_group(graph, _entry("cap-1", {"n": 1}, _EMBEDDING)))

    (entry,) = store.read_entries(graph)
    assert entry.embedding is not None
    assert [_bits(value) for value in entry.embedding] == [_bits(value) for value in _EMBEDDING]
    ((_, embedding_hash),) = _entry_hashes(prov, graph)
    assert embedding_hash is not None
    ((stored_hash, kind, byte_length, body),) = _payload_rows(prov, embedding_hash)
    assert kind == PAYLOAD_KIND_EMBEDDING
    assert byte_length == len(body) == _EMBEDDING_BYTES_PER_VALUE * len(_EMBEDDING)
    assert stored_hash == payload_hash(body)
    assert struct.unpack(f"<{len(_EMBEDDING)}Q", body) == tuple(_bits(v) for v in _EMBEDDING)


def test_entry_without_embedding_reads_back_with_none(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()

    store.append_group_standalone(_group(graph, _entry("cap-1", {"n": 1})))

    (entry,) = store.read_entries(graph)
    assert entry.embedding is None
    assert entry.content == {"n": 1}


def test_small_content_stays_inline_and_stores_no_payload_row(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    store.append_group_standalone(_group(graph, _entry("cap-1", {"n": 1})))

    assert _entry_hashes(prov, graph) == [(None, None)]


def test_large_content_is_stored_as_a_json_payload_and_read_back_equal(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    content = _large_content()

    store.append_group_standalone(_group(graph, _entry("cap-1", content)))

    (entry,) = store.read_entries(graph)
    assert entry.content == content
    ((content_hash, _),) = _entry_hashes(prov, graph)
    assert content_hash is not None
    ((_, kind, _, body),) = _payload_rows(prov, content_hash)
    assert kind == PAYLOAD_KIND_JSON
    assert body == canonical_json_bytes(content)


def test_identical_large_payload_appended_twice_is_stored_once_and_both_entries_share_the_hash(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    content = _large_content(marker=f"dedup-{uuid.uuid4().hex}")

    store.append_group_standalone(_group(graph, _entry("cap-1", content)))
    store.append_group_standalone(_group(graph, _entry("cap-2", dict(reversed(content.items())))))

    ((first_hash, _), (second_hash, _)) = _entry_hashes(prov, graph)
    assert first_hash is not None
    assert first_hash == second_hash
    assert len(_payload_rows(prov, first_hash)) == 1
    assert [entry.content for entry in store.read_entries(graph)] == [content, content]


def test_identical_large_payload_twice_in_one_group_is_stored_once(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    content = _large_content(marker=f"same-group-{uuid.uuid4().hex}")

    store.append_group_standalone(_group(graph, _entry("cap-1", content), _entry("cap-2", content)))

    ((first_hash, _), (second_hash, _)) = _entry_hashes(prov, graph)
    assert first_hash is not None
    assert first_hash == second_hash
    assert len(_payload_rows(prov, first_hash)) == 1


def test_identical_embeddings_on_different_entries_share_one_payload_row(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph_a, graph_b = _graph(), _graph()
    embedding = (0.1, 1 / 3, uuid.uuid4().int / 2**130)

    store.append_group_standalone(_group(graph_a, _entry("cap-1", {"n": 1}, embedding)))
    store.append_group_standalone(_group(graph_b, _entry("cap-2", {"n": 2}, embedding)))

    ((_, hash_a),) = _entry_hashes(prov, graph_a)
    ((_, hash_b),) = _entry_hashes(prov, graph_b)
    assert hash_a is not None
    assert hash_a == hash_b
    assert len(_payload_rows(prov, hash_a)) == 1


def test_concurrent_identical_payload_appends_store_one_row(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    content = _large_content(marker=f"race-{uuid.uuid4().hex}")
    graphs = [_graph() for _ in range(_CONCURRENT_APPENDERS)]

    def append(graph: str) -> None:
        store.append_group_standalone(_group(graph, _entry("cap-1", content)))

    with ThreadPoolExecutor(max_workers=_CONCURRENT_APPENDERS) as pool:
        for future in [pool.submit(append, graph) for graph in graphs]:
            future.result()

    hashes = {_entry_hashes(prov, graph)[0][0] for graph in graphs}
    assert len(hashes) == 1
    (only_hash,) = hashes
    assert only_hash is not None
    assert len(_payload_rows(prov, only_hash)) == 1


def test_state_role_can_insert_payload_without_update_privilege(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    content = _large_content(marker=f"privileges-{uuid.uuid4().hex}")

    # Appending the same payload twice exercises ON CONFLICT DO NOTHING under the real grants.
    store.append_group_standalone(_group(graph, _entry("cap-1", content)))
    store.append_group_standalone(_group(graph, _entry("cap-2", content)))

    with prov.as_state() as conn:
        privileges = conn.execute(
            "SELECT has_table_privilege('graph_log.payloads', 'INSERT'), "
            "has_table_privilege('graph_log.payloads', 'UPDATE')"
        ).fetchone()
    assert privileges == (True, False)


def test_payload_hash_is_computed_by_the_store_never_supplied_by_the_caller() -> None:
    assert "payload_hash" not in GraphLogEntryDraft.model_fields
    assert "content_payload_hash" not in GraphLogEntryDraft.model_fields
    assert "embedding_payload_hash" not in GraphLogEntryDraft.model_fields


def test_content_that_is_not_json_serialisable_is_rejected_before_any_sql(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    with pytest.raises(GraphLogPayloadError):
        store.append_group_standalone(_group(graph, _entry("cap-1", {"x": math.nan})))

    assert store.last_position(graph) == 0


def test_failed_group_with_new_payloads_leaves_no_payload_rows_behind(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    content = _large_content(marker=f"rolled-back-{uuid.uuid4().hex}")
    failing = GraphLogEntryDraft(name="Capability\x00", identity="x", content={"k": 1})

    with pytest.raises(GraphLogPersistenceError):
        store.append_group_standalone(_group(graph, _entry("cap-1", content), failing))

    assert _payload_rows(prov, payload_hash(canonical_json_bytes(content))) == []


def test_append_logs_payload_and_deduplicated_counts_but_never_content_or_embeddings(
    provisioned_graph_log: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = _store(provisioned_graph_log, emitter)
    content = _large_content(marker=f"logging-{uuid.uuid4().hex}")
    group = _group(_graph(), _entry("cap-1", content, (0.1, 0.2)), _entry("cap-2", content))

    store.append_group_standalone(group)
    emitter.flush()

    (line,) = [line for line in read_lines(log_path) if line.get("action") == "append_group"]
    assert (line["payload_count"], line["deduplicated_count"]) == (3, 1)
    assert "content" not in line
    assert "embedding" not in line
    assert "0.1" not in str(line)
