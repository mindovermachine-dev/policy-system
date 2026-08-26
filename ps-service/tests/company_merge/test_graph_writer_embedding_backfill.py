"""Tests for `ps_service.company_merge.graph_writer.
backfill_canonical_embeddings` (PLAN_REVIEWED.md §10 Increment 11, backfill
half -- B2's fix): the `WHERE n.embedding IS NULL`-guarded embedding
backfill writer for already-existing canonical nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.company_merge.graph_writer import backfill_canonical_embeddings


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


_EXPECTED_QUERY = "MATCH (n:Obligation {id: $id}) WHERE n.embedding IS NULL SET n.embedding = $embedding"


def test_backfill_writes_exact_three_clause_query() -> None:
    graph = _FakeGraph()

    backfill_canonical_embeddings(
        graph, kind="Obligation", embeddings={"obligation_1": (0.1, 0.2)}
    )

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == _EXPECTED_QUERY
    assert "MATCH (n:Obligation {id: $id})" in call.query
    assert "WHERE n.embedding IS NULL" in call.query
    assert "SET n.embedding = $embedding" in call.query
    # Clauses appear in that order.
    assert call.query.index("MATCH") < call.query.index("WHERE") < call.query.index("SET")
    assert call.params == {"id": "obligation_1", "embedding": [0.1, 0.2]}


def test_backfill_writes_one_call_per_id_with_own_embedding() -> None:
    graph = _FakeGraph()
    embeddings = {
        "obligation_1": (0.1, 0.2),
        "obligation_2": (0.3, 0.4),
        "obligation_3": (0.5, 0.6),
    }

    backfill_canonical_embeddings(graph, kind="Obligation", embeddings=embeddings)

    assert len(graph.calls) == 3
    calls_by_id = {call.params["id"]: call for call in graph.calls if call.params is not None}
    assert set(calls_by_id) == {"obligation_1", "obligation_2", "obligation_3"}
    for node_id, embedding in embeddings.items():
        call = calls_by_id[node_id]
        assert call.query == _EXPECTED_QUERY
        assert call.params == {"id": node_id, "embedding": list(embedding)}


def test_backfill_with_empty_embeddings_writes_nothing() -> None:
    graph = _FakeGraph()

    backfill_canonical_embeddings(graph, kind="Obligation", embeddings={})

    assert graph.calls == []


def test_backfill_capability_kind_writes_capability_label() -> None:
    graph = _FakeGraph()

    backfill_canonical_embeddings(
        graph, kind="Capability", embeddings={"capability_1": (0.9,)}
    )

    assert graph.calls[0].query == (
        "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL SET n.embedding = $embedding"
    )


def test_backfill_called_twice_with_identical_input_issues_identical_calls() -> None:
    """Calling `backfill_canonical_embeddings` twice with the identical
    `embeddings` dict against the same fake graph produces identical calls
    both times -- call-shape identity. The real no-op-on-second-run EFFECT
    depends on real FalkorDB state (the `WHERE n.embedding IS NULL` guard),
    proven live elsewhere -- this only proves the writer doesn't behave
    conditionally differently on a second call."""
    graph = _FakeGraph()
    embeddings = {"obligation_1": (0.1, 0.2), "obligation_2": (0.3, 0.4)}

    backfill_canonical_embeddings(graph, kind="Obligation", embeddings=embeddings)
    backfill_canonical_embeddings(graph, kind="Obligation", embeddings=embeddings)

    assert len(graph.calls) == 4
    first_run, second_run = graph.calls[:2], graph.calls[2:]
    assert first_run == second_run
