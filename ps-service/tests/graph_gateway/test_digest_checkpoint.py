"""A requested checkpoint records the canonical digest at the group's last position (#207 S1).

AC-RD-003: WHEN a group commits and a checkpoint is requested THEN the digest at that group's
last position is recorded in the log; WHEN none is requested THEN none is recorded. The digest
scan is chunked by internal-id cursor so a big graph is never held in memory at once (CHANGES A4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import GatewayRig, InMemoryGraph
from ps_service.graph_gateway.digest import DigestSettings, canonical_digest
from ps_service.graph_gateway.models import GroupOutcome, MutationGroup, UpsertNode

if TYPE_CHECKING:
    from ps_service.ingestion.falkordb_client import GraphQueryResult

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _node(node_id: str, name: str = "n") -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": name})


def _submit(
    rig: GatewayRig, *nodes: UpsertNode, checkpoint_requested: bool = False
) -> GroupOutcome:
    return rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=nodes,
            checkpoint_requested=checkpoint_requested,
        )
    )


@dataclass
class _CountingGraph:
    """Wraps the fake graph and remembers how many rows each query answered."""

    inner: InMemoryGraph
    answered: list[int] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        result = self.inner.query(q, params)
        self.answered.append(len(result.result_set))
        return result


def test_checkpoint_requested_group_records_digest_at_the_last_position() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, _node("cap-1"), _node("cap-2"), checkpoint_requested=True)

    assert outcome.status == "applied"
    assert outcome.checkpoint == "recorded"
    assert outcome.last_position == 2
    assert outcome.checkpoint_position == 2
    checkpoint = rig.store.checkpoints[(_GRAPH, 2)]
    assert _DIGEST.match(checkpoint.canonical_digest)
    assert checkpoint.canonical_digest == canonical_digest(rig.graphs.open(_GRAPH))


def test_group_without_checkpoint_request_records_no_checkpoint() -> None:
    rig = GatewayRig()

    outcome = _submit(rig, _node("cap-1"))

    assert outcome.checkpoint == "not_requested"
    assert outcome.checkpoint_position is None
    assert rig.store.checkpoints == {}


def test_digest_changes_when_the_graph_content_changes() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1", "a"))
    before = canonical_digest(rig.graphs.open(_GRAPH))

    _submit(rig, _node("cap-1", "b"))

    assert canonical_digest(rig.graphs.open(_GRAPH)) != before


def test_node_scan_is_chunked_and_chunk_size_does_not_change_the_digest() -> None:
    rig = GatewayRig()
    _submit(rig, *(_node(f"cap-{i:02d}", f"name-{i}") for i in range(20)))
    graph = rig.graphs.open(_GRAPH)
    counting = _CountingGraph(graph)

    chunked = canonical_digest(counting, DigestSettings(node_chunk_rows=7))

    assert chunked == canonical_digest(graph, DigestSettings(node_chunk_rows=5000))
    assert max(counting.answered) <= 7
    assert counting.answered[:3] == [7, 7, 6]  # then one edge scan, which answers nothing


def test_digest_with_a_replay_sentinel_present_does_not_raise_and_ignores_it() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    graph = rig.graphs.open(_GRAPH)
    without_sentinel = canonical_digest(graph)

    graph.nodes[("GraphReplayState", "sentinel")] = {"position": 3, "state": "in_progress"}

    assert canonical_digest(graph) == without_sentinel


def test_the_empty_graph_has_a_digest() -> None:
    assert _DIGEST.match(canonical_digest(GatewayRig().graphs.open(_GRAPH)))


@pytest.mark.parametrize("rows", [0, -3])
def test_digest_settings_reject_a_chunk_size_below_one(rows: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        DigestSettings(node_chunk_rows=rows)


def test_the_default_node_chunk_holds_a_statement_under_the_server_timeout_with_embeddings() -> (
    None
):
    # A row with a 3,072-double embedding costs about 4 ms of server time (exact floats
    # included); FalkorDB cancels a query at 1000 ms by default. 250 rows timed out on a real
    # CRA x10 graph, 50 rows take about 0.2 s.
    assert DigestSettings().node_chunk_rows <= 50
