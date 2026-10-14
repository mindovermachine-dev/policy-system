"""`backfill_edge_identity` stamps the identity-less edges of a legacy graph.

The reader is the graph handle itself (read-only queries); the write goes through the gateway as
one `StampEdgeIdentity` group under the caller's audit event id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway._fakes import FakeQueryResult, GatewayRig
from ps_service.graph_gateway.cypher import UNIDENTIFIED_EDGE_SCAN
from ps_service.graph_gateway.edge_identity_backfill import backfill_edge_identity
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

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
_GRAPH = "compliance"


def _legacy_rig() -> GatewayRig:
    """Three nodes logged through the gateway; two edges between them carry no identity."""
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=(
                UpsertNode(label="Policy", id="p-1"),
                UpsertNode(label="Standard", id="s-1"),
                UpsertNode(label="Standard", id="s-2"),
            ),
        )
    )
    graph = rig.graphs.open(_GRAPH)
    graph.seed_legacy_edge(("HAS", "Policy", "p-1", "Standard", "s-1"), {"weight": 1})
    graph.seed_legacy_edge(("HAS", "Policy", "p-1", "Standard", "s-2"), {})
    return rig


def _backfill(rig: GatewayRig, *, emitter: LogEmitter | None = None):
    return backfill_edge_identity(
        rig.gateway,
        _GRAPH,
        _AUDIT_EVENT_ID,
        reader=rig.graphs.open(_GRAPH),
        emitter=emitter,
    )


def test_every_identity_less_edge_is_stamped_in_one_group_keeping_its_properties() -> None:
    rig = _legacy_rig()
    logged = rig.store.last_position(_GRAPH)

    outcome = _backfill(rig)

    assert (outcome.status, outcome.stamped, outcome.skipped) == ("applied", 2, 0)
    assert rig.graphs.open(_GRAPH).edges == {
        ("HAS", "Policy", "p-1", "Standard", "s-1", "HAS:p-1->s-1"): {
            "weight": 1,
            "identity": "HAS:p-1->s-1",
        },
        ("HAS", "Policy", "p-1", "Standard", "s-2", "HAS:p-1->s-2"): {"identity": "HAS:p-1->s-2"},
    }
    group = rig.store.groups[-1]
    assert group.audit_event_id == _AUDIT_EVENT_ID
    assert rig.store.last_position(_GRAPH) == logged + 2  # one entry per edge, one group


def test_a_second_run_finds_nothing_to_do_and_logs_nothing() -> None:
    rig = _legacy_rig()
    _backfill(rig)
    logged = rig.store.last_position(_GRAPH)
    groups = len(rig.store.groups)

    outcome = _backfill(rig)

    assert (outcome.status, outcome.stamped) == ("unchanged", 0)
    assert rig.store.last_position(_GRAPH) == logged
    assert len(rig.store.groups) == groups


def test_the_scan_is_read_only_and_paged() -> None:
    rig = _legacy_rig()

    _backfill(rig)

    scans = [q for q in rig.graphs.open(_GRAPH).queries if q.text == UNIDENTIFIED_EDGE_SCAN]
    assert scans
    assert all(q.kind == "read" for q in scans)
    assert all(word not in UNIDENTIFIED_EDGE_SCAN for word in ("MERGE", "SET", "DELETE"))


class _ScriptedScan:
    """A read handle answering the scan with fixed rows, honouring the `$after` cursor."""

    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        del q
        after = cast("int", (params or {})["after"])
        return FakeQueryResult([row for row in self._rows if cast("int", row[0]) > after])


def test_duplicate_identity_less_edges_between_two_nodes_are_stamped_by_one_primitive() -> None:
    rig = _legacy_rig()
    duplicates = _ScriptedScan(
        [
            [1, "HAS", ["Policy"], "p-1", ["Standard"], "s-1"],
            [2, "HAS", ["Policy"], "p-1", ["Standard"], "s-1"],
        ]
    )

    outcome = backfill_edge_identity(rig.gateway, _GRAPH, _AUDIT_EVENT_ID, reader=duplicates)

    assert outcome.stamped == 1


def test_an_edge_the_gateway_cannot_name_is_skipped_and_counted() -> None:
    rig = _legacy_rig()
    unlisted = _ScriptedScan([[1, "NOT_A_TYPE", ["Policy"], "p-1", ["Standard"], "s-1"]])

    outcome = backfill_edge_identity(rig.gateway, _GRAPH, _AUDIT_EVENT_ID, reader=unlisted)

    assert (outcome.status, outcome.stamped, outcome.skipped) == ("unchanged", 0, 1)


def test_the_backfill_emits_one_semantic_log_entry_per_run(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _legacy_rig()

    _backfill(rig, emitter=emitter)
    _backfill(rig, emitter=emitter)
    emitter.flush()
    lines = [
        line for line in read_lines(log_path) if line.get("action") == "edge_identity_backfill"
    ]

    assert [(line["outcome"], line.get("entry_count")) for line in lines] == [
        ("applied", 2),
        ("unchanged", 0),
    ]
    assert all(line["graph"] == _GRAPH for line in lines)
    assert "p-1" not in str(lines)


def test_the_scan_pages_through_more_edges_than_one_chunk() -> None:
    rig = _legacy_rig()

    outcome = backfill_edge_identity(
        rig.gateway, _GRAPH, _AUDIT_EVENT_ID, reader=rig.graphs.open(_GRAPH), chunk_rows=1
    )

    assert outcome.stamped == 2
    scans = [q for q in rig.graphs.open(_GRAPH).queries if q.text == UNIDENTIFIED_EDGE_SCAN]
    assert len(scans) == 3  # one row, one row, then an empty page


def test_an_unreadable_reply_is_refused() -> None:
    rig = _legacy_rig()
    garbage = _ScriptedScan([[1, "HAS"]])

    with pytest.raises(UnexpectedGraphReplyError):
        backfill_edge_identity(rig.gateway, _GRAPH, _AUDIT_EVENT_ID, reader=garbage)
