"""`postgres_live` tests for appending and reading log groups (issue #205, slice 3).

Runs the store as the real `ps_state` role against a scratch PS Postgres provisioned by the real
Helm init script and the privileged provisioning path (see `persistence/provisioned_postgres.py`).
Covers AC-BI-002 (atomic group, contiguous per-graph sequence, no partial group) and AC-BI-003
(concurrent appends stay unique and gap-free), including rolled-back appenders.

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Protocol

import psycopg
import pytest
from psycopg import sql

from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.graph_gateway.models import AppendedGroup, GraphLogEntryDraft, GraphLogGroupDraft
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

    from ps_service.logging import LogEmitter

pytestmark = pytest.mark.postgres_live

_WAIT_FOR_LOCK_SECONDS = 10.0
_WORKERS = 8
_GROUPS_PER_WORKER = 5
_ENTRIES_PER_GROUP = 3


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_REPEATABLE_READ_MESSAGE = "graph log appends require READ COMMITTED"
_NO_TRANSACTION_MESSAGE = "graph log appends require an open transaction"


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _entries(count: int, *, prefix: str = "e") -> tuple[GraphLogEntryDraft, ...]:
    return tuple(
        GraphLogEntryDraft(name="Capability", identity=f"{prefix}-{index}", content={"n": index})
        for index in range(count)
    )


def _group(graph: str, count: int = _ENTRIES_PER_GROUP, *, prefix: str = "e") -> GraphLogGroupDraft:
    return GraphLogGroupDraft(graph=graph, entries=_entries(count, prefix=prefix))


def _entry_that_fails_in_postgres(name_prefix: str) -> GraphLogEntryDraft:
    """Non-blank (passes validation) but Postgres text cannot hold NUL: a real mid-group failure."""
    return GraphLogEntryDraft(name=f"{name_prefix}\x00", identity="x", content={"k": 1})


def _failing_group(graph: str) -> GraphLogGroupDraft:
    """Three entries; the second one fails inside the database after the first was written."""
    first, _, third = _entries(3, prefix="f")
    return GraphLogGroupDraft(
        graph=graph, entries=(first, _entry_that_fails_in_postgres("Capability"), third)
    )


def _store(prov: Provisioned, emitter: LogEmitter | None = None) -> PsycopgGraphLogStore:
    return PsycopgGraphLogStore(prov.state_config(), emitter=emitter)


def _count(prov: Provisioned, table: str, graph: str) -> int:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM graph_log.{} WHERE graph = %s").format(
                sql.Identifier(table)
            ),
            (graph,),
        ).fetchone()
    assert row is not None
    count = row[0]
    assert isinstance(count, int)
    return count


def _wait_for_waiting_advisory_lock(prov: Provisioned) -> None:
    """Block until some session is queued behind an advisory lock (the contending appender)."""
    deadline = time.monotonic() + _WAIT_FOR_LOCK_SECONDS
    with prov.superuser_connect(prov.state_db) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ).fetchone()
            if row is not None and row[0]:
                return
            time.sleep(0.02)
    pytest.fail("no appender queued behind the graph advisory lock")


def test_append_group_assigns_contiguous_positions_starting_at_one(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()

    appended = store.append_group_standalone(_group(graph, 3))

    assert (appended.graph, appended.first_position, appended.last_position) == (graph, 1, 3)
    entries = store.read_entries(graph)
    assert [entry.position for entry in entries] == [1, 2, 3]
    assert [entry.identity for entry in entries] == ["e-0", "e-1", "e-2"]
    assert {entry.group_id for entry in entries} == {appended.group_id}
    assert entries[0].content == {"n": 0}
    assert store.last_position(graph) == 3


def test_second_group_continues_the_sequence_for_the_same_graph(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()
    store.append_group_standalone(_group(graph, 2))

    second = store.append_group_standalone(_group(graph, 2, prefix="s"))

    assert (second.first_position, second.last_position) == (3, 4)
    assert [entry.position for entry in store.read_entries(graph)] == [1, 2, 3, 4]
    assert [entry.position for entry in store.read_entries(graph, after_position=2)] == [3, 4]


def test_sequences_are_independent_per_graph(provisioned_graph_log: Provisioned) -> None:
    store = _store(provisioned_graph_log)
    graph_a, graph_b = _graph(), _graph()
    store.append_group_standalone(_group(graph_a, 4))

    appended = store.append_group_standalone(_group(graph_b, 2))

    assert (appended.first_position, appended.last_position) == (1, 2)
    assert store.last_position(graph_a) == 4
    assert store.last_position("never-written-graph") == 0


def test_failed_insert_in_the_middle_of_a_group_leaves_no_partial_group(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    marker_graph = _graph()

    with connect_from_config(prov.state_config()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO graph_log.applied_markers (graph, applied_position) VALUES (%s, 0)",
            (marker_graph,),
        )
        with pytest.raises(GraphLogPersistenceError):
            store.append_group(cur, _failing_group(graph))
        # The caller's transaction is still usable and holds nothing of the failed group.
        cur.execute("SELECT count(*) FROM graph_log.groups WHERE graph = %s", (graph,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM graph_log.entries WHERE graph = %s", (graph,))
        assert cur.fetchone() == (0,)
        # Gap-free after rollback: the next group takes the positions the failed one would have.
        retried = store.append_group(cur, _group(graph, 2))

    assert (retried.first_position, retried.last_position) == (1, 2)
    assert _count(prov, "applied_markers", marker_graph) == 1
    assert _count(prov, "groups", graph) == 1
    assert [entry.position for entry in store.read_entries(graph)] == [1, 2]


def test_standalone_append_of_a_failing_group_commits_nothing(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()

    with pytest.raises(GraphLogPersistenceError):
        store.append_group_standalone(_failing_group(graph))

    assert _count(provisioned_graph_log, "groups", graph) == 0
    assert _count(provisioned_graph_log, "entries", graph) == 0


def _append_many(
    store: PsycopgGraphLogStore, graph: str, worker: int, *, fail_every_third: bool
) -> list[Exception]:
    failures: list[Exception] = []
    for index in range(_GROUPS_PER_WORKER):
        sequence_number = worker * _GROUPS_PER_WORKER + index
        should_fail = fail_every_third and sequence_number % 3 == 0
        group = _failing_group(graph) if should_fail else _group(graph, prefix=f"w{worker}g{index}")
        try:
            store.append_group_standalone(group)
        except GraphLogPersistenceError as exc:
            failures.append(exc)
        else:
            assert not should_fail
    return failures


def _run_concurrent_appends(
    store: PsycopgGraphLogStore, graph: str, *, fail_every_third: bool
) -> list[Exception]:
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [
            pool.submit(_append_many, store, graph, worker, fail_every_third=fail_every_third)
            for worker in range(_WORKERS)
        ]
    return [exc for future in futures for exc in future.result()]


def _assert_positions_gap_free_and_groups_contiguous(
    store: PsycopgGraphLogStore, graph: str, expected_entries: int
) -> None:
    entries = store.read_entries(graph)
    assert [entry.position for entry in entries] == list(range(1, expected_entries + 1))
    by_group: dict[uuid.UUID, list[int]] = {}
    for entry in entries:
        by_group.setdefault(entry.group_id, []).append(entry.position)
    for positions in by_group.values():
        assert len(positions) == _ENTRIES_PER_GROUP
        assert positions == list(range(positions[0], positions[0] + _ENTRIES_PER_GROUP))


def test_concurrent_appends_to_same_graph_stay_unique_and_gap_free(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()

    failures = _run_concurrent_appends(store, graph, fail_every_third=False)

    assert failures == []
    total = _WORKERS * _GROUPS_PER_WORKER * _ENTRIES_PER_GROUP
    _assert_positions_gap_free_and_groups_contiguous(store, graph, total)


def test_concurrent_appends_with_some_rolled_back_groups_stay_gap_free(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()
    total_groups = _WORKERS * _GROUPS_PER_WORKER
    failing_groups = len(range(0, total_groups, 3))

    failures = _run_concurrent_appends(store, graph, fail_every_third=True)

    assert len(failures) == failing_groups
    committed_entries = (total_groups - failing_groups) * _ENTRIES_PER_GROUP
    _assert_positions_gap_free_and_groups_contiguous(store, graph, committed_entries)


def test_concurrent_rollback_releases_numbers_to_waiting_appender(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    with connect_from_config(prov.state_config()) as holder, holder.cursor() as cur:
        store.append_group(cur, _group(graph, 2, prefix="held"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(store.append_group_standalone, _group(graph, 3, prefix="wait"))
            _wait_for_waiting_advisory_lock(prov)
            assert not waiting.done()
            holder.rollback()
            appended = waiting.result(timeout=_WAIT_FOR_LOCK_SECONDS)

    assert (appended.first_position, appended.last_position) == (1, 3)
    assert [entry.identity for entry in store.read_entries(graph)] == [
        "wait-0",
        "wait-1",
        "wait-2",
    ]


def test_append_group_in_read_committed_caller_transaction_holds_lock_until_commit(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    with connect_from_config(prov.state_config()) as holder, holder.cursor() as cur:
        first = store.append_group(cur, _group(graph, 2, prefix="held"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting: Future[AppendedGroup] = pool.submit(
                store.append_group_standalone, _group(graph, 2, prefix="wait")
            )
            _wait_for_waiting_advisory_lock(prov)
            assert not waiting.done()
            holder.commit()
            second = waiting.result(timeout=_WAIT_FOR_LOCK_SECONDS)

    assert (first.first_position, first.last_position) == (1, 2)
    assert (second.first_position, second.last_position) == (3, 4)


def test_append_group_in_repeatable_read_transaction_is_rejected_with_clear_error(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()
    conn = connect_from_config(prov.state_config())
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ

    with conn, conn.cursor() as cur, pytest.raises(GraphLogPersistenceError) as raised:
        store.append_group(cur, _group(graph))

    assert str(raised.value) == _REPEATABLE_READ_MESSAGE
    assert _count(prov, "groups", graph) == 0


def test_append_group_on_an_autocommit_connection_is_rejected_with_clear_error(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _graph()

    conn = connect_from_config(prov.state_config())
    conn.autocommit = True

    with conn, conn.cursor() as cur, pytest.raises(GraphLogPersistenceError) as raised:
        store.append_group(cur, _group(graph))

    assert str(raised.value) == _NO_TRANSACTION_MESSAGE
    assert _count(prov, "groups", graph) == 0


def test_different_graphs_do_not_serialize_each_other(provisioned_graph_log: Provisioned) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    held_graph, other_graph = _graph(), _graph()

    with connect_from_config(prov.state_config()) as holder, holder.cursor() as cur:
        store.append_group(cur, _group(held_graph, 1))
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(store.append_group_standalone, _group(other_graph, 2))
            appended = other.result(timeout=_WAIT_FOR_LOCK_SECONDS)
        holder.rollback()

    assert (appended.first_position, appended.last_position) == (1, 2)


def test_successful_append_emits_one_semantic_log_entry_without_content(
    provisioned_graph_log: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = _store(provisioned_graph_log, emitter)
    graph = _graph()

    appended = store.append_group_standalone(_group(graph, 2))
    emitter.flush()

    entries = [line for line in read_lines(log_path) if line.get("action") == "append_group"]
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["component"], entry["outcome"]) == ("graph_gateway", "success")
    assert entry["graph"] == graph
    assert entry["group_id"] == str(appended.group_id)
    assert (entry["first_position"], entry["last_position"], entry["entry_count"]) == (1, 2, 2)
    assert "content" not in entry


def test_failed_append_emits_one_failure_log_entry_with_the_error_class_only(
    provisioned_graph_log: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = _store(provisioned_graph_log, emitter)

    with pytest.raises(GraphLogPersistenceError):
        store.append_group_standalone(_failing_group(_graph()))
    emitter.flush()

    entries = [line for line in read_lines(log_path) if line.get("action") == "append_group"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["error_class"] == "DataError"
    assert "\x00" not in str(entries[0])
