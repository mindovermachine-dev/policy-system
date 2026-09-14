"""Tests for `ps_service.company_merge.pending_review.persist_pending_reviews`
(issue #35, Slice 1, AC-BI-001/AC-BI-002): a `PendingReview` node is persisted
in FalkorDB for every `NearMissPair` surfaced during a Company Merge dedup
pass, carrying the pair's incoming id/text, nearest-existing id/text, and
similarity score verbatim -- so a later review workflow (Slices 2-4) never
needs to re-query embeddings to render or act on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ps_service.company_merge.models import NearMissPair
from ps_service.company_merge.pending_review import persist_pending_reviews


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
    """Satisfies `GraphHandle` structurally, recording every call it receives."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([])


def _near_miss(
    incoming_id: str = "capability_incoming_abc",
    nearest_existing_id: str = "capability_existing_xyz",
) -> NearMissPair:
    return NearMissPair(
        incoming_id=incoming_id,
        incoming_text="Report the incident to the authority.",
        nearest_existing_id=nearest_existing_id,
        nearest_existing_text="Conduct a risk assessment.",
        similarity=0.62,
    )


def test_persist_pending_reviews_creates_node_with_pair_fields_verbatim() -> None:
    graph = _FakeGraph()
    pair = _near_miss()

    persist_pending_reviews(graph, (pair,), kind="Capability")

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "CREATE (r:PendingReview" in call.query
    assert call.params is not None
    params = call.params
    assert params["kind"] == "Capability"
    assert params["status"] == "pending"
    assert params["incoming_id"] == pair.incoming_id
    assert params["incoming_text"] == pair.incoming_text
    assert params["nearest_existing_id"] == pair.nearest_existing_id
    assert params["nearest_existing_text"] == pair.nearest_existing_text
    assert params["similarity"] == pair.similarity

    review_id = params["id"]
    assert isinstance(review_id, str)
    assert review_id.startswith("review_")

    created_at = params["created_at"]
    assert isinstance(created_at, str)
    datetime.fromisoformat(created_at)  # round-trips without raising


def test_persist_pending_reviews_no_near_misses_issues_no_calls() -> None:
    graph = _FakeGraph()

    persist_pending_reviews(graph, (), kind="Capability")

    assert graph.calls == []


def test_persist_pending_reviews_issues_one_call_per_pair_with_distinct_ids() -> None:
    graph = _FakeGraph()
    pair_a = _near_miss(
        incoming_id="capability_incoming_a", nearest_existing_id="capability_existing_a"
    )
    pair_b = _near_miss(
        incoming_id="capability_incoming_b", nearest_existing_id="capability_existing_b"
    )

    persist_pending_reviews(graph, (pair_a, pair_b), kind="Policy")

    assert len(graph.calls) == 2
    ids = {call.params["id"] for call in graph.calls if call.params is not None}
    assert len(ids) == 2
    assert all(call.params is not None and call.params["kind"] == "Policy" for call in graph.calls)
