"""The Graph Write Gateway's structured logging, across every path (issue #206, S14; AC-BI-013).

Only identifiers and positions may be logged: never a payload, an embedding, a label, an
identity, a property key or value, exception text, a host or a credential. One driver exercises
every path of the gateway (success, no-op, rejection, retry, fail-closed, pending, blocked,
catch-up, reconciler, in-transaction, recovery) with sentinel values planted in each place a
leak could come from, then the tests read the log file back.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig, SteppedWait
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphUnavailableError,
    GraphWriteRejectedError,
)
from ps_service.graph_gateway.gateway_log import ALLOWED_LOG_FIELDS, emit_gateway_event
from ps_service.graph_gateway.label_allow_list import ALLOWED_RELATIONSHIP_TYPES
from ps_service.graph_gateway.models import (
    ExpectedPosition,
    MergeProperty,
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_WAIT_SECONDS = 5.0
_LABEL = "Capability"
_EDGE_TYPE = min(ALLOWED_RELATIONSHIP_TYPES)
_SECRET = "s3cr3t-token-9f1"
_HOST = "10.9.8.7"
_OFF_LIST_LABEL = "SecretOffListLabel"
_NODE_ID = "secret-node-id-77"
_PROPERTY_KEY = "secret_property_key"
_EMBEDDING = (0.987654321, 0.123456789)
_SENTINELS = (
    _SECRET,
    _HOST,
    _OFF_LIST_LABEL,
    _NODE_ID,
    _PROPERTY_KEY,
    "0.987654321",
    "0.123456789",
    _LABEL,
    _EDGE_TYPE,
    "edge-identity-55",
)
_DOWN = redis.exceptions.ConnectionError(f"Error 111 connecting to {_HOST}:6379. {_SECRET}")
_REFUSED = redis.exceptions.ResponseError(f"unknown command near {_SECRET}")
_ENVELOPE_KEYS = frozenset({"component", "action", "outcome", "timestamp"})
_NO_GRAPH_ACTIONS = frozenset({"reconciler_stop"})


def _node(node_id: str = _NODE_ID, label: str = _LABEL) -> UpsertNode:
    return UpsertNode(
        label=label,
        id=node_id,
        properties={_PROPERTY_KEY: _SECRET},
        embedding=_EMBEDDING,
    )


def _group(graph: str, *primitives: object, **kwargs: object) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=primitives or (_node(),),  # pyright: ignore[reportArgumentType]
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def _wait_until(condition: object) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition() and time.monotonic() < deadline:  # pyright: ignore[reportCallIssue,reportOperatorIssue]
        time.sleep(0.005)
    assert condition()  # pyright: ignore[reportCallIssue]


def _drive_every_path(emitter: LogEmitter) -> None:
    """Run the gateway through each path with the sentinels planted; ends with a quiet rig."""
    stepped = SteppedWait(timeout=_WAIT_SECONDS)
    rig = GatewayRig(emitter, reconciler_wait=stepped)
    gateway = rig.gateway
    try:
        # success, then the same group again (no-op, unchanged)
        gateway.submit_group(_group("g_ok"))
        gateway.submit_group(_group("g_ok"))
        # in-transaction path
        transaction = rig.store.begin()
        audit_event_id = transaction.record_audit()
        in_tx = MutationGroup(
            graph="g_tx",
            audit_event_id=audit_event_id,
            primitives=(_node("tx-node"),),
        )
        with gateway.submit_group_in_transaction(transaction.cursor, in_tx) as staged:
            transaction.commit()
            staged.complete()
        # rejections: off-list label, stale precondition, absent merge target
        for rejected in (
            _group("g_ok", _node(label=_OFF_LIST_LABEL)),
            _group("g_ok", _node("other"), preconditions=(ExpectedPosition(position=99),)),
            _group(
                "g_ok",
                MergeProperty(label=_LABEL, id="absent", properties={_PROPERTY_KEY: _SECRET}),
            ),
        ):
            with pytest.raises(GraphWriteRejectedError):
                gateway.submit_group(rejected)
        # retry that recovers, then retry that ends fail-closed
        rig.graphs.open("g_retry").fail_on_read(_DOWN, times=2)
        gateway.submit_group(_group("g_retry"))
        rig.graphs.open("g_down").fail_on_read(_DOWN)
        with pytest.raises(GraphUnavailableError):
            gateway.submit_group(_group("g_down"))
        # permanent failure, then the blocked graph refusing a new group
        rig.graphs.open("g_blocked").fail_on_write(_REFUSED)
        with pytest.raises(GraphApplyError):
            gateway.submit_group(_group("g_blocked"))
        with pytest.raises(GraphApplyBlockedError):
            gateway.submit_group(_group("g_blocked"))
        # edge between two nodes
        gateway.submit_group(
            _group(
                "g_ok",
                _node("edge-target"),
                UpsertEdge(
                    type=_EDGE_TYPE,
                    identity="edge-identity-55",
                    source=NodeRef(label=_LABEL, id=_NODE_ID),
                    target=NodeRef(label=_LABEL, id="edge-target"),
                    properties={_PROPERTY_KEY: _SECRET},
                ),
            )
        )
        # pending, then the reconciler pass that applies it
        pending = rig.graphs.open("g_pending")
        pending.fail_on_write(_DOWN)
        assert gateway.submit_group(_group("g_pending")).status == "committed_apply_pending"
        stepped.await_wait()
        pending.heal()
        stepped.resume()
        _wait_until(lambda: not gateway.is_reconciling)
        # explicit catch-up of a second pending graph
        lagging = rig.graphs.open("g_catchup")
        lagging.fail_on_write(_DOWN)
        gateway.submit_group(_group("g_catchup"))
        lagging.heal()
        gateway.catch_up("g_catchup")
    finally:
        stepped.stopped.set()
        stepped.resume()
        rig.close()
    # startup recovery on a fresh process over the same kind of backlog
    recovery_rig = GatewayRig(emitter)
    recovery_rig.graphs.open("g_recover").fail_on_write(_DOWN)
    recovery_rig.gateway.submit_group(_group("g_recover"))
    recovery_rig.close()
    recovery_rig.graphs.open("g_recover").heal()
    recovery_rig.restart().recover()


@pytest.fixture
def gateway_log(make_emitter: MakeEmitter, read_lines: ReadLines) -> list[dict[str, object]]:
    emitter, log_path = make_emitter()
    _drive_every_path(emitter)
    emitter.flush()
    return read_lines(log_path)


def test_no_log_entry_in_any_path_contains_payload_embedding_label_or_secret(
    gateway_log: list[dict[str, object]],
) -> None:
    assert gateway_log, "the driver logged nothing, so the sweep proved nothing"
    for line in gateway_log:
        text = json.dumps(line)
        for sentinel in _SENTINELS:
            assert sentinel not in text, (sentinel, line)


def test_the_sweep_reaches_every_gateway_path(gateway_log: list[dict[str, object]]) -> None:
    seen = {(line["action"], line["outcome"]) for line in gateway_log}

    assert seen >= {
        ("apply_group", "success"),
        ("apply_group", "unchanged"),
        ("apply_group", "pending"),
        ("apply_group", "failure"),
        ("submit_rejected", "failure"),
        ("apply_retry", "retry"),
        ("catch_up", "success"),
        ("reconciler_pass", "success"),
        ("startup_recovery", "success"),
    }


def test_every_gateway_log_entry_carries_only_the_permitted_fields(
    gateway_log: list[dict[str, object]],
) -> None:
    for line in gateway_log:
        if line["component"] != "graph_gateway":
            continue
        assert set(line) <= _ENVELOPE_KEYS | ALLOWED_LOG_FIELDS, line


def test_every_gateway_log_entry_names_graph_and_sequence_where_applicable(
    gateway_log: list[dict[str, object]],
) -> None:
    gateway_lines = [line for line in gateway_log if line["component"] == "graph_gateway"]
    assert gateway_lines
    for line in gateway_lines:
        if line["action"] in _NO_GRAPH_ACTIONS:
            continue
        assert isinstance(line.get("graph"), str), line
        if line["action"] == "apply_group" and line["outcome"] in {"success", "pending"}:
            assert isinstance(line.get("first_position"), int), line
            assert isinstance(line.get("last_position"), int), line
            assert isinstance(line.get("audit_event_id"), str), line
        if line["action"] == "apply_retry":
            assert isinstance(line.get("attempt"), int), line
            assert isinstance(line.get("backoff_seconds"), float), line
    permanent = [
        line
        for line in gateway_lines
        if line["action"] == "apply_group"
        and line["outcome"] == "failure"
        and line.get("error_class") == "GraphApplyError"
    ]
    assert permanent
    assert all(isinstance(line.get("first_position"), int) for line in permanent)


@pytest.mark.parametrize("field", ["content", "label", "embedding", "message", "host", "identity"])
def test_a_log_field_outside_the_permitted_set_is_refused(field: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        emit_gateway_event("apply_group", "success", {"graph": "g", field: "x"}, emitter=None)
