"""`postgres_live` tests for the applied marker and digest checkpoint (issue #205, slice 6).

The applied marker is the only mutable table of the graph log: it may advance, never move back
and never pass the last logged position. A digest checkpoint is insert-only: recording a second
one at the same position is rejected, never an overwrite. Runs the store as the real `ps_state`
role against a scratch PS Postgres (AC-BI-005, AC-BI-010).

Deselected by default -- run with `uv run pytest -m postgres_live`; needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` and `psql` on `PATH`.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, LiteralString, Protocol

import psycopg
import pytest
from pydantic import ValidationError

from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.graph_gateway.models import (
    AppliedMarker,
    DigestCheckpoint,
    GraphLogEntryDraft,
    GraphLogGroupDraft,
)
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

    from ps_service.logging import LogEmitter

pytestmark = pytest.mark.postgres_live

_GRAPH_LOG_TABLES = ("payloads", "groups", "entries", "checkpoints", "applied_markers")
_DIGEST = "sha256:" + "ab" * 32


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


def _graph() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"


def _store(prov: Provisioned, emitter: LogEmitter | None = None) -> PsycopgGraphLogStore:
    return PsycopgGraphLogStore(prov.state_config(), emitter=emitter)


def _logged_graph(store: PsycopgGraphLogStore, entry_count: int) -> str:
    """Return a new graph whose log holds `entry_count` entries (positions 1..entry_count)."""
    graph = _graph()
    entries = tuple(
        GraphLogEntryDraft(name="Capability", identity=f"cap-{index}", content={"n": index})
        for index in range(entry_count)
    )
    store.append_group_standalone(GraphLogGroupDraft(graph=graph, entries=entries))
    return graph


def test_applied_marker_defaults_to_zero_for_a_graph_with_no_marker(
    provisioned_graph_log: Provisioned,
) -> None:
    assert _store(provisioned_graph_log).read_applied_position(_graph()) == 0


def test_applied_marker_advances_and_never_moves_backwards(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 5)

    first = store.advance_applied_position(graph, 2)
    assert first == AppliedMarker(graph=graph, applied_position=2)
    assert store.advance_applied_position(graph, 5).applied_position == 5
    assert store.advance_applied_position(graph, 3).applied_position == 5  # not moved back
    assert store.advance_applied_position(graph, 5).applied_position == 5  # idempotent
    assert store.read_applied_position(graph) == 5


def test_applied_marker_cannot_pass_the_last_logged_position(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 3)
    store.advance_applied_position(graph, 2)

    with pytest.raises(GraphLogPersistenceError):
        store.advance_applied_position(graph, 4)

    assert store.read_applied_position(graph) == 2


def test_applied_marker_of_a_graph_with_no_log_cannot_leave_zero(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _graph()

    with pytest.raises(GraphLogPersistenceError):
        store.advance_applied_position(graph, 1)

    assert store.read_applied_position(graph) == 0


def test_applied_marker_rejects_a_negative_position_before_any_sql(
    provisioned_graph_log: Provisioned,
) -> None:
    with pytest.raises(ValidationError):
        _store(provisioned_graph_log).advance_applied_position(_graph(), -1)


def test_concurrent_advances_end_at_the_highest_requested_position(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 12)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(store.advance_applied_position, graph, p) for p in range(1, 13)]
        for future in futures:
            future.result()

    assert store.read_applied_position(graph) == 12


def test_digest_checkpoint_round_trips(provisioned_graph_log: Provisioned) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 4)

    recorded = store.record_digest_checkpoint(graph, 4, _DIGEST)

    expected = DigestCheckpoint(graph=graph, position=4, canonical_digest=_DIGEST)
    assert recorded == expected
    assert store.read_digest_checkpoint(graph, 4) == expected


def test_reading_a_checkpoint_that_was_never_recorded_returns_none(
    provisioned_graph_log: Provisioned,
) -> None:
    assert _store(provisioned_graph_log).read_digest_checkpoint(_graph(), 1) is None


def test_recording_a_second_checkpoint_at_the_same_position_is_rejected_not_overwritten(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 2)
    store.record_digest_checkpoint(graph, 2, _DIGEST)

    with pytest.raises(GraphLogPersistenceError) as raised:
        store.record_digest_checkpoint(graph, 2, "sha256:" + "cd" * 32)

    assert isinstance(raised.value.__cause__, psycopg.errors.UniqueViolation)
    checkpoint = store.read_digest_checkpoint(graph, 2)
    assert checkpoint is not None
    assert checkpoint.canonical_digest == _DIGEST


def test_checkpoints_at_different_positions_of_one_graph_coexist(
    provisioned_graph_log: Provisioned,
) -> None:
    store = _store(provisioned_graph_log)
    graph = _logged_graph(store, 3)

    store.record_digest_checkpoint(graph, 1, "digest-one")
    store.record_digest_checkpoint(graph, 3, "digest-three")

    first = store.read_digest_checkpoint(graph, 1)
    third = store.read_digest_checkpoint(graph, 3)
    assert (first and first.canonical_digest, third and third.canonical_digest) == (
        "digest-one",
        "digest-three",
    )


@pytest.mark.parametrize("digest", ["", "   "])
def test_checkpoint_with_a_blank_digest_is_rejected_before_any_sql(
    provisioned_graph_log: Provisioned, digest: str
) -> None:
    with pytest.raises(ValidationError):
        _store(provisioned_graph_log).record_digest_checkpoint(_graph(), 1, digest)


@pytest.mark.parametrize(
    "rewrite",
    [
        "UPDATE graph_log.checkpoints SET canonical_digest = 'forged' WHERE graph = %(graph)s",
        "DELETE FROM graph_log.checkpoints WHERE graph = %(graph)s",
        "TRUNCATE graph_log.checkpoints",
    ],
    ids=["update", "delete", "truncate"],
)
def test_state_role_cannot_delete_or_update_a_digest_checkpoint_directly(
    provisioned_graph_log: Provisioned, rewrite: LiteralString
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _logged_graph(store, 1)
    store.record_digest_checkpoint(graph, 1, _DIGEST)

    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(rewrite, {"graph": graph})

    checkpoint = store.read_digest_checkpoint(graph, 1)
    assert checkpoint is not None
    assert checkpoint.canonical_digest == _DIGEST


def test_state_role_cannot_delete_an_applied_marker_even_though_it_may_update_it(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    store = _store(prov)
    graph = _logged_graph(store, 1)
    store.advance_applied_position(graph, 1)

    with prov.as_state() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM graph_log.applied_markers WHERE graph = %s", (graph,))

    assert store.read_applied_position(graph) == 1


def test_applied_marker_is_the_only_immutable_schema_table_with_update_granted_to_state_role(
    provisioned_graph_log: Provisioned,
) -> None:
    prov = provisioned_graph_log
    with prov.superuser_connect(prov.state_db) as conn:
        grants = {
            (str(table), str(privilege))
            for table, privilege in conn.execute(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'graph_log' AND grantee = %s",
                (prov.state_user,),
            ).fetchall()
        }

    expected = {
        (table, privilege) for table in _GRAPH_LOG_TABLES for privilege in ("SELECT", "INSERT")
    }
    expected.add(("applied_markers", "UPDATE"))
    assert grants == expected


def test_marker_and_checkpoint_operations_each_emit_one_semantic_log_entry(
    provisioned_graph_log: Provisioned, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = _store(provisioned_graph_log, emitter)
    graph = _logged_graph(store, 2)

    store.advance_applied_position(graph, 2)
    store.record_digest_checkpoint(graph, 2, _DIGEST)
    with pytest.raises(GraphLogPersistenceError):
        store.record_digest_checkpoint(graph, 2, _DIGEST)
    emitter.flush()

    lines = read_lines(log_path)
    advanced = [line for line in lines if line.get("action") == "advance_applied_position"]
    recorded = [line for line in lines if line.get("action") == "record_digest_checkpoint"]
    assert [(line["outcome"], line["graph"], line["position"]) for line in advanced] == [
        ("success", graph, 2)
    ]
    assert [line["outcome"] for line in recorded] == ["success", "failure"]
    assert recorded[1]["error_class"] == "UniqueViolation"
    assert all(line["component"] == "graph_gateway" for line in advanced + recorded)
