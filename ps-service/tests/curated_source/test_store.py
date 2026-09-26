"""Tests for `ps_service.curated_source.store` (issue #125, Slice 3):
AC-BI-012 (`set_override` persists), AC-BI-014 (`reset_override` clears),
AC-BI-015 (`get_override` reads back).

Mirrors `tests/company_merge/test_pending_review_resolve.py`'s own
hand-written `GraphHandle`/`GraphQueryResult` fake convention (no mocking
library, PLAN.md §0.6) for the scripted-CRUD-shape tests below -- these can
only prove "this query string/these params were sent," not that the Cypher
itself round-trips through a real graph. That is what the `falkordb_live`-
marked test at the bottom proves, against a real FalkorDB instance
(CHANGES.md/BASELINE.md: this sandbox now has one reachable at
127.0.0.1:6379, so this test runs for real rather than being deselected).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions

from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.config import load_config
from ps_service.curated_source.errors import CuratedSourceOverridePersistenceError
from ps_service.curated_source.store import get_override, reset_override, set_override

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally; returns one scripted result per call, in order."""

    def __init__(self, results: list[list[list[object]]] | None = None) -> None:
        self.calls: list[_RecordedCall] = []
        self._results = list(results or [])
        self._error: Exception | None = None

    def raise_on_next_call(self, error: Exception) -> None:
        self._error = error

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        rows = self._results.pop(0) if self._results else []
        return _FakeQueryResult(cast("list[object]", rows))


def test_get_override_returns_none_when_no_override_persisted() -> None:
    """AC-BI-015 (default half): an empty result set means "no override"."""
    graph = _FakeGraph(results=[[]])

    result = get_override(graph)

    assert result is None
    assert len(graph.calls) == 1
    assert "MATCH (o:CatalogSourceOverride {id: $id}) RETURN" in graph.calls[0].query
    assert graph.calls[0].params == {"id": "singleton"}


def test_get_override_returns_the_persisted_url() -> None:
    """AC-BI-015 (override half): a stored row's URL is returned verbatim."""
    graph = _FakeGraph(results=[[["https://example.com/override"]]])

    result = get_override(graph)

    assert result == "https://example.com/override"


def test_set_override_writes_singleton_node_with_url_and_updated_at(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-012: `MERGE` on the singleton id, `SET url`/`updated_at`."""
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=[[]])

    set_override(graph, "https://example.com/override", emitter=emitter)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "MERGE (o:CatalogSourceOverride {id: $id})" in call.query
    assert "SET o.url = $url" in call.query
    assert "o.updated_at = $updated_at" in call.query
    assert call.params is not None
    assert call.params["id"] == "singleton"
    assert call.params["url"] == "https://example.com/override"
    assert isinstance(call.params["updated_at"], str)
    assert call.params["updated_at"]


def test_set_override_then_get_override_round_trips_on_the_same_fake_graph(
    make_emitter: MakeEmitter,
) -> None:
    """The CRUD shape composes: a set followed by a get on the same graph sees the write.

    `_FakeGraph` only scripts pre-canned results, so this simulates the
    round trip by scripting the follow-up `get_override` read with the same
    URL `set_override` was just called with -- the actual persistence
    round-trip proof is the `falkordb_live` test below, against a real graph.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=[[], [["https://example.com/override"]]])

    set_override(graph, "https://example.com/override", emitter=emitter)
    result = get_override(graph)

    assert result == "https://example.com/override"


def test_reset_override_deletes_the_singleton_node(make_emitter: MakeEmitter) -> None:
    """AC-BI-014: `DELETE` on the singleton id."""
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=[[]])

    reset_override(graph, emitter=emitter)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "MATCH (o:CatalogSourceOverride {id: $id}) DELETE o" in call.query
    assert call.params == {"id": "singleton"}


def test_set_override_emits_structured_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Semantic logging (PLAN.md task point 9): a set is recorded with the new URL."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=[[]])

    set_override(graph, "https://example.com/override", emitter=emitter)
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["component"] == "curated_source"
    assert entries[0]["action"] == "set_catalog_source_override"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["url"] == "https://example.com/override"


def test_reset_override_emits_structured_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Semantic logging (PLAN.md task point 9): a reset is recorded even when a no-op."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=[[]])

    reset_override(graph, emitter=emitter)
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["component"] == "curated_source"
    assert entries[0]["action"] == "reset_catalog_source_override"
    assert entries[0]["outcome"] == "success"


def test_set_override_raises_persistence_error_on_falkordb_write_failure() -> None:
    """A `redis.exceptions.RedisError` from the write is translated, mirrors `pending_review.py`."""
    graph = _FakeGraph()
    graph.raise_on_next_call(redis.exceptions.ConnectionError("connection refused"))

    with pytest.raises(CuratedSourceOverridePersistenceError):
        set_override(graph, "https://example.com/override")


def test_reset_override_raises_persistence_error_on_falkordb_write_failure() -> None:
    """Mirrors the `set_override` case immediately above, for `reset_override`."""
    graph = _FakeGraph()
    graph.raise_on_next_call(redis.exceptions.ConnectionError("connection refused"))

    with pytest.raises(CuratedSourceOverridePersistenceError):
        reset_override(graph)


def test_get_override_propagates_falkordb_read_failure_unwrapped() -> None:
    """`get_override` is a plain, unwrapped read (mirrors `pending_review.list_pending_reviews`):
    a FalkorDB failure propagates as-is -- `resolve.resolve_effective_source`'s
    D-FAILOPEN fallback is what turns this into "no override," not this module.
    """
    graph = _FakeGraph()
    graph.raise_on_next_call(redis.exceptions.ConnectionError("connection refused"))

    with pytest.raises(redis.exceptions.ConnectionError):
        get_override(graph)


# --- falkordb_live (issue #125, Slice 3: REQUIRED, not optional per PLAN.md) ---

_LIVE_TEST_GRAPH = "policy_system_slice3_catalog_source_live_test"


@pytest.mark.falkordb_live
def test_set_get_reset_round_trip_against_a_real_falkordb_instance(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-012/014/015 against a REAL FalkorDB instance.

    This is the one test in this file that can actually prove the `MERGE`/
    `MATCH`+`RETURN`/`MATCH`+`DELETE` Cypher round-trips through a real
    graph -- the scripted fakes above cannot execute Cypher.

    Writes into a dedicated, disposable graph name
    (`policy_system_slice3_catalog_source_live_test`), NEVER the real,
    shared `policy_system` graph -- mirrors
    `test_pending_review_resolve.py::test_resolve_merge_repoints_edges_and_deletes_loser_live`'s
    own established convention (CLAUDE.md: a database that already holds
    real regulatory data is not something a test mutates without a human's
    separately-obtained permission). The disposable graph is deleted in a
    `finally` block regardless of outcome, including a failing run, plus a
    defensive pre-clean in case a prior failed run left residue.
    """
    emitter, _log_path = make_emitter()
    db = connect_from_config(load_config())

    existing_graphs = set(db.list_graphs())
    if _LIVE_TEST_GRAPH in existing_graphs:
        db.select_graph(_LIVE_TEST_GRAPH).delete()

    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)

        # No override persisted yet.
        assert get_override(graph) is None

        # Set: persists and is immediately readable.
        set_override(graph, "https://example.com/first-override", emitter=emitter)
        assert get_override(graph) == "https://example.com/first-override"

        # Set again: MERGE updates the same singleton node, not a duplicate.
        set_override(graph, "https://example.com/second-override", emitter=emitter)
        assert get_override(graph) == "https://example.com/second-override"
        count_result = graph.query("MATCH (o:CatalogSourceOverride) RETURN count(o)")
        count_rows = cast("list[list[object]]", count_result.result_set)
        assert count_rows[0][0] == 1

        # Reset: clears it.
        reset_override(graph, emitter=emitter)
        assert get_override(graph) is None

        # Reset again (no-op, already clear): does not raise.
        reset_override(graph, emitter=emitter)
        assert get_override(graph) is None
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
        remaining_graphs = set(db.list_graphs())
        assert _LIVE_TEST_GRAPH not in remaining_graphs
