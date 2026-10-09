"""Pre-append retry, then fail closed (issue #206, S10; AC-BI-009, AC-BI-013).

Transient failures (redis connection and timeout errors, Postgres unavailable) are retried with a
bounded, deterministic backoff (0.2, 0.4, 0.8 s; the sleep is injected). Past the budget the
caller gets a fixed-message error and nothing is logged. Permanent failures are not retried.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.errors import (
    GRAPH_LOG_UNAVAILABLE_MESSAGE,
    GraphApplyError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
    GraphUnavailableError,
)
from ps_service.graph_gateway.models import ExpectedPosition, MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_BACKOFF = [0.2, 0.4, 0.8]
_HOST_TEXT = "Error 111 connecting to 10.1.2.3:6379. Connection refused."


def _group(*, expected_position: int | None = None) -> MutationGroup:
    preconditions = (
        () if expected_position is None else (ExpectedPosition(position=expected_position),)
    )
    return MutationGroup(
        graph=_GRAPH,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label="Capability", id="cap-1", properties={"name": "a"}),),
        preconditions=preconditions,
    )


def test_unreachable_graph_before_append_retries_with_backoff_then_fails_closed_sanitized() -> None:
    rig = GatewayRig()
    cause = redis.exceptions.ConnectionError(_HOST_TEXT)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_read(cause)

    with pytest.raises(GraphUnavailableError) as raised:
        rig.gateway.submit_group(_group())

    assert rig.sleeps == _BACKOFF
    assert rig.store.entries == {}
    assert rig.store.append_attempts == 0
    assert str(raised.value) == "The graph store is temporarily unavailable."
    assert "10.1.2.3" not in repr(raised.value)
    assert raised.value.__cause__ is cause
    assert not is_healthy(FALKORDB)


@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.ConnectionError("down"),
        redis.exceptions.BusyLoadingError("loading"),
        redis.exceptions.TimeoutError("slow"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_connection_error_is_retried(error: Exception) -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_read(error, times=1)

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "applied"
    assert rig.sleeps == [0.2]


def test_transient_graph_failure_before_append_recovers_within_budget() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_read(redis.exceptions.ConnectionError(_HOST_TEXT), times=3)

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "applied"
    assert rig.sleeps == _BACKOFF
    assert rig.store.last_position(_GRAPH) == 1


def test_response_error_is_not_retried_and_raises_graph_apply_error() -> None:
    rig = GatewayRig()
    cause = redis.exceptions.ResponseError("Invalid input near 10.1.2.3 secret-token")
    rig.graphs.open(_GRAPH).fail_on_read(cause)

    with pytest.raises(GraphApplyError) as raised:
        rig.gateway.submit_group(_group())

    assert rig.sleeps == []
    assert rig.store.entries == {}
    assert (raised.value.graph, raised.value.position) == (_GRAPH, None)
    assert "secret-token" not in str(raised.value)
    assert raised.value.__cause__ is cause
    assert is_healthy(FALKORDB)


def test_log_store_unavailable_fails_closed_with_existing_sanitized_error() -> None:
    rig = GatewayRig()
    rig.store.fail_appends()

    with pytest.raises(GraphLogUnavailableError) as raised:
        rig.gateway.submit_group(_group())

    assert str(raised.value) == GRAPH_LOG_UNAVAILABLE_MESSAGE
    assert rig.sleeps == _BACKOFF
    assert rig.store.append_attempts == 4
    assert rig.store.entries == {}
    assert not rig.graphs.open(_GRAPH).nodes


def test_postgres_unreachable_before_append_retries_then_raises_graph_log_unavailable() -> None:
    rig = GatewayRig()
    rig.store.fail_reads()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.submit_group(_group(expected_position=0))

    assert rig.sleeps == _BACKOFF
    assert rig.store.append_attempts == 0
    assert rig.graphs.open(_GRAPH).queries == []


def test_a_postgres_blip_during_append_recovers_within_budget() -> None:
    rig = GatewayRig()
    rig.store.fail_appends(times=2)

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "applied"
    assert rig.sleeps == [0.2, 0.4]
    assert rig.store.last_position(_GRAPH) == 1


def test_a_graph_log_persistence_error_is_never_retried() -> None:
    rig = GatewayRig()
    rig.store.fail_appends(error=GraphLogPersistenceError("commit outcome unknown"))

    with pytest.raises(GraphLogPersistenceError):
        rig.gateway.submit_group(_group())

    assert rig.store.append_attempts == 1
    assert rig.sleeps == []


def test_retry_events_carry_graph_attempt_backoff_and_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.graphs.open(_GRAPH).fail_on_read(redis.exceptions.ConnectionError(_HOST_TEXT))

    with pytest.raises(GraphUnavailableError):
        rig.gateway.submit_group(_group())
    emitter.flush()

    retries = [line for line in read_lines(log_path) if line.get("action") == "apply_retry"]
    assert [(line["attempt"], line["backoff_seconds"]) for line in retries] == [
        (1, 0.2),
        (2, 0.4),
        (3, 0.8),
    ]
    assert {(line["graph"], line["error_class"], line["component"]) for line in retries} == {
        (_GRAPH, "ConnectionError", "graph_gateway")
    }
    assert "10.1.2.3" not in json.dumps(retries)


def _assert_failure_logged(
    emitter: LogEmitter, log_path: Path, read_lines: ReadLines, error_class: str
) -> None:
    emitter.flush()
    (line,) = [entry for entry in read_lines(log_path) if entry.get("action") == "apply_group"]
    assert (line["outcome"], line["graph"], line["error_class"]) == (
        "failure",
        _GRAPH,
        error_class,
    )
    assert "10.1.2.3" not in json.dumps(line)


def test_a_submit_that_finds_the_graph_down_is_logged_with_graph_and_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.graphs.open(_GRAPH).fail_on_read(redis.exceptions.ConnectionError(_HOST_TEXT))

    with pytest.raises(GraphUnavailableError):
        rig.gateway.submit_group(_group())

    _assert_failure_logged(emitter, log_path, read_lines, "GraphUnavailableError")


def test_a_submit_that_finds_the_log_down_is_logged_with_graph_and_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.store.fail_appends()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.submit_group(_group())

    _assert_failure_logged(emitter, log_path, read_lines, "GraphLogUnavailableError")
