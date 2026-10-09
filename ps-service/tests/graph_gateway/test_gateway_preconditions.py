"""Caller preconditions on a group (issue #206 S5, AC-BI-002).

Only `ExpectedPosition` exists (D3): the caller states the log position it last saw, and the
gateway refuses the group, logging nothing, if the log has moved. The check reads the log store
only, never the graph.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.errors import GraphWriteRejectedError, StaleGraphStateError
from ps_service.graph_gateway.models import ExpectedPosition, MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _group(node_id: str, *preconditions: ExpectedPosition) -> MutationGroup:
    return MutationGroup(
        graph=_GRAPH,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label="Capability", id=node_id),),
        preconditions=preconditions,
    )


def _rig_with_one_logged_group() -> GatewayRig:
    rig = GatewayRig()
    rig.gateway.submit_group(_group("cap-1"))
    rig.events.clear()
    return rig


def test_failed_expected_position_precondition_logs_nothing_and_raises_stale_state_error() -> None:
    rig = _rig_with_one_logged_group()

    with pytest.raises(StaleGraphStateError) as raised:
        rig.gateway.submit_group(_group("cap-2", ExpectedPosition(position=0)))

    assert isinstance(raised.value, GraphWriteRejectedError)
    assert (raised.value.graph, raised.value.expected_position, raised.value.actual_position) == (
        _GRAPH,
        0,
        1,
    )
    assert rig.store.last_position(_GRAPH) == 1
    assert rig.store.read_applied_position(_GRAPH) == 1
    assert rig.events == []  # nothing logged, and the graph was not even read
    assert ("Capability", "cap-2") not in rig.graphs.open(_GRAPH).nodes


def test_satisfied_preconditions_proceed() -> None:
    rig = _rig_with_one_logged_group()

    outcome = rig.gateway.submit_group(_group("cap-2", ExpectedPosition(position=1)))

    assert (outcome.first_position, outcome.last_position, outcome.status) == (2, 2, "applied")


def test_expected_position_zero_means_an_empty_log() -> None:
    rig = GatewayRig()

    outcome = rig.gateway.submit_group(_group("cap-1", ExpectedPosition(position=0)))

    assert outcome.first_position == 1


def test_gateway_adds_no_precondition_of_its_own() -> None:
    rig = _rig_with_one_logged_group()
    rig.gateway.submit_group(_group("cap-2"))  # moves the log on behind this caller's back

    outcome = rig.gateway.submit_group(_group("cap-3"))

    assert outcome.status == "applied"
    assert (outcome.first_position, outcome.last_position) == (3, 3)
    assert _group("cap-4").preconditions == ()


def test_negative_expected_position_is_rejected_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        ExpectedPosition(position=-1)


def test_stale_rejection_is_logged_with_the_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.gateway.submit_group(_group("cap-1"))

    with pytest.raises(StaleGraphStateError):
        rig.gateway.submit_group(_group("cap-2", ExpectedPosition(position=0)))
    emitter.flush()

    (line,) = [entry for entry in read_lines(log_path) if entry.get("action") == "submit_rejected"]
    assert (line["graph"], line["error_class"]) == (_GRAPH, "StaleGraphStateError")
    assert "cap-2" not in json.dumps(line)
