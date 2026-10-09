"""Batched, idempotent apply through the public gateway (issue #206, S8; AC-BI-005, AC-BI-008).

Every test submits a group through `GraphWriteGateway.submit_group` and asserts on the queries the
fake graph received (tagged by template and label), the graph state and the applied marker.
"""

from __future__ import annotations

import contextlib
import math
from itertools import zip_longest
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import (
    DeleteNode,
    MergeProperty,
    MutationGroup,
    UpsertNode,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph, RecordedQuery
    from ps_service.graph_gateway.models import Primitive

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_BATCH = 500


def _group(*primitives: Primitive) -> MutationGroup:
    return MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)


def _upsert(label: str, node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label=label, id=node_id, properties=properties)


def _tagged(graph: InMemoryGraph, kind: str, template: str, label: str) -> list[RecordedQuery]:
    return [
        query
        for query in graph.queries
        if (query.kind, query.template) == (kind, template) and query.labels[:1] == (label,)
    ]


def _row_count(query: RecordedQuery) -> int:
    rows = query.params["rows"]
    assert isinstance(rows, list)
    return len(cast("list[object]", rows))


def _submit_tolerating_apply_failure(rig: GatewayRig, group: MutationGroup) -> None:
    """Submit `group`; a FalkorDB failure while applying is an expected part of the scenario."""
    with contextlib.suppress(redis.exceptions.RedisError):
        rig.gateway.submit_group(group)


def test_applying_n_nodes_issues_at_most_ceil_n_over_batch_queries_per_label() -> None:
    rig = GatewayRig(settings=GatewaySettings(batch_size=_BATCH))
    capabilities = [_upsert("Capability", f"cap-{i}") for i in range(601)]
    policies = [_upsert("Policy", f"pol-{i}") for i in range(600)]
    interleaved = [
        node for pair in zip_longest(capabilities, policies) for node in pair if node is not None
    ]
    assert len(interleaved) == 1201

    outcome = rig.gateway.submit_group(_group(*interleaved))

    graph = rig.graphs.open(_GRAPH)
    assert outcome.status == "applied"
    assert len(graph.nodes) == 1201
    for label, count in (("Capability", 601), ("Policy", 600)):
        limit = math.ceil(count / _BATCH)
        assert len(_tagged(graph, "write", "upsert_node", label)) <= limit
        assert len(_tagged(graph, "read", "node_state", label)) <= limit


def test_interleaved_labels_A_B_A_issue_one_write_query_per_label() -> None:
    rig = GatewayRig()

    rig.gateway.submit_group(
        _group(_upsert("Capability", "a1"), _upsert("Policy", "b1"), _upsert("Capability", "a2"))
    )

    graph = rig.graphs.open(_GRAPH)
    assert len(_tagged(graph, "write", "upsert_node", "Capability")) == 1
    assert len(_tagged(graph, "write", "upsert_node", "Policy")) == 1
    assert rig.store.marker_history == [(_GRAPH, 3)]


def test_a_repeated_id_splits_the_run_so_the_later_row_lands_last() -> None:
    rig = GatewayRig()

    rig.gateway.submit_group(
        _group(
            _upsert("Capability", "x", version=1),
            _upsert("Policy", "y"),
            _upsert("Capability", "x", version=2),
        )
    )

    graph = rig.graphs.open(_GRAPH)
    assert graph.nodes[("Capability", "x")]["version"] == 2
    assert [q.template for q in graph.queries if q.kind == "write"] == ["upsert_node"] * 3
    assert rig.store.marker_history == [(_GRAPH, 2), (_GRAPH, 3)]


def test_batches_preserve_log_order_across_ops() -> None:
    rig = GatewayRig()

    rig.gateway.submit_group(
        _group(
            _upsert("Capability", "x", version=1),
            DeleteNode(label="Capability", id="x"),
            _upsert("Capability", "x", version=3),
            MergeProperty(label="Capability", id="x", properties={"merged": True}),
        )
    )

    graph = rig.graphs.open(_GRAPH)
    assert [q.template for q in graph.queries if q.kind == "write"] == [
        "upsert_node",
        "delete_node",
        "upsert_node",
        "merge_property",
    ]
    assert graph.nodes[("Capability", "x")] == {"version": 3, "merged": True}
    assert rig.store.marker_history == [(_GRAPH, 1), (_GRAPH, 2), (_GRAPH, 3), (_GRAPH, 4)]


def test_replaying_applied_entries_issues_no_queries() -> None:
    rig = GatewayRig()
    group = _group(_upsert("Capability", "x"), _upsert("Policy", "y"))
    rig.gateway.submit_group(group)
    graph = rig.graphs.open(_GRAPH)
    graph.queries.clear()
    rig.restart()

    outcome = rig.gateway.submit_group(group)

    assert outcome.status == "unchanged"
    assert [q for q in graph.queries if q.kind != "read"] == []
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 2


def test_apply_resumes_after_last_applied_position_after_midrun_failure() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(1, redis.exceptions.ConnectionError())

    _submit_tolerating_apply_failure(
        rig,
        _group(
            _upsert("Capability", "x", version=1),
            MergeProperty(label="Capability", id="x", properties={"merged": True}),
            DeleteNode(label="Capability", id="gone"),
        ),
    )

    assert rig.store.last_position(_GRAPH) == 2
    assert rig.store.read_applied_position(_GRAPH) == 1
    assert graph.nodes[("Capability", "x")] == {"version": 1}
    graph.heal()
    graph.queries.clear()

    rig.gateway.submit_group(_group(_upsert("Policy", "y")))

    assert [q.template for q in graph.queries if q.kind == "write"] == [
        "merge_property",
        "upsert_node",
    ]
    assert graph.nodes[("Capability", "x")] == {"version": 1, "merged": True}
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 3


@pytest.mark.parametrize("batch_size", [1, 2, 7])
def test_batch_size_bounds_the_rows_of_every_write_query(batch_size: int) -> None:
    rig = GatewayRig(settings=GatewaySettings(batch_size=batch_size))

    rig.gateway.submit_group(_group(*(_upsert("Capability", f"c{i}") for i in range(10))))

    writes = [q for q in rig.graphs.open(_GRAPH).queries if q.kind == "write"]
    sizes = [_row_count(query) for query in writes]
    assert sum(sizes) == 10
    assert max(sizes) <= batch_size
    assert len(writes) == math.ceil(10 / batch_size)
