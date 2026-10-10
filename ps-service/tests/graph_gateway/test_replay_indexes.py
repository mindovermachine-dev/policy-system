"""Replay sets up each label's `id` index once and writes in batches (#207 S13, AC-RD-008).

The spike behind the issue measured 9.6 s with the index in place against 67 s and 207 s without
it or with one query per row. Wall clock is proved live; here the deterministic part is pinned:
the statements a replay sends.
"""

from __future__ import annotations

import math

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_LABELS = ("Capability", "Policy", "Standard", "Control")


def _rig_with_log(primitives: tuple[UpsertNode, ...], settings: GatewaySettings) -> GatewayRig:
    """A rig whose log holds one group per primitive; its graph is then wiped and reset."""
    rig = GatewayRig(settings=settings)
    for primitive in primitives:
        rig.gateway.submit_group(
            MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=(primitive,))
        )
    _wipe(rig)
    return rig


def _wipe(rig: GatewayRig) -> None:
    graph = rig.graphs.open(_GRAPH)
    graph.flush()
    graph.queries.clear()


def _nodes(count: int, labels: tuple[str, ...] = _LABELS) -> tuple[UpsertNode, ...]:
    return tuple(
        UpsertNode(label=labels[number % len(labels)], id=f"n-{number}", properties={})
        for number in range(count)
    )


def _count(rig: GatewayRig, template: str) -> int:
    return sum(1 for query in rig.graphs.open(_GRAPH).queries if query.template == template)


def test_replay_creates_each_label_index_once_and_lists_indexes_once() -> None:
    rig = _rig_with_log(_nodes(48), GatewaySettings(replay_page_size=2))  # 24 pages, 4 labels

    report = rig.restart().replay_graph(_GRAPH)

    assert report.pages == 24
    assert _count(rig, "create_index") == len(_LABELS)
    assert _count(rig, "list_indexes") == 1


def test_a_resumed_replay_lists_the_indexes_once_and_creates_none_that_exist() -> None:
    rig = _rig_with_log(_nodes(8), GatewaySettings(replay_page_size=2))
    rig.restart().replay_graph(_GRAPH)  # builds the indexes and finishes
    graph = rig.graphs.open(_GRAPH)
    graph.replay_state = (4, "in_progress", "", -1)  # as if it had died after position 4
    graph.queries.clear()

    rig.restart().replay_graph(_GRAPH)

    assert _count(rig, "list_indexes") == 1
    assert _count(rig, "create_index") == 0


def test_replay_issues_at_most_ceil_n_over_batch_size_write_queries_per_run_and_label() -> None:
    rig = GatewayRig(settings=GatewaySettings(batch_size=5))
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=_nodes(23, ("Capability",)),
        )
    )
    _wipe(rig)

    rig.restart().replay_graph(_GRAPH)

    assert _count(rig, "upsert_node") == math.ceil(23 / 5)


def test_replay_never_issues_a_query_per_entry() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=_nodes(300, ("Capability",)),
        )
    )
    _wipe(rig)

    rig.restart().replay_graph(_GRAPH)

    assert _count(rig, "upsert_node") == 1  # 300 entries, one page, one batch of 500
