"""Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`
(PLAN.md Slice 5.4, D6): semantic match via artifact-supplied embeddings.

Per CHANGES.md MA1: this function never calls `route_embedding` -- every
embedding it scores with is either artifact-supplied (`incoming_embeddings`)
or already cached on an existing canonical node
(`ExistingCanonicalNode.embedding`). An existing canonical node with no
cached embedding is excluded from scoring entirely ("skip, don't fetch",
D6) -- never raises, never calls anything network-shaped -- and, when one or
more entries were skipped this way, exactly one aggregate
`outcome="warning"` log line is emitted (MA1's fix, also closing OQ4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.models import BaselineNode

_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedSingleTenantGraph:
    """Satisfies `GraphHandle` structurally -- answers
    `read_existing_canonical_index`'s one read query.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _capability(node_id: str, name: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": name})


def test_semantic_match_resolves_via_cosine_similarity_against_cached_embedding(
    make_emitter: MakeEmitter,
) -> None:
    """An incoming Capability with a pre-computed embedding scores above
    threshold against an existing canonical node that already carries a
    cached embedding -- resolves to that canonical id via
    `cosine_similarity` alone.
    """
    emitter, _log_path = make_emitter()
    existing_id = "capability_data_encryption"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Data Encryption Capability", [1.0, 0.0]]]
    )
    incoming_id = "incoming_capability_alpha"
    incoming_nodes = (_capability(incoming_id, "Data Encryption at Rest"),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "semantic"
    assert resolution.incoming_id == incoming_id
    assert resolution.canonical_id == existing_id


def test_existing_node_missing_cached_embedding_is_excluded_from_scoring(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """An existing canonical node lacking a cached embedding is never
    scored against, never raises, and never triggers any network-shaped
    call -- the incoming node mints new since no scorable candidate exists.
    A `skipped_missing_embedding_count` aggregate warning is logged.
    """
    emitter, log_path = make_emitter()
    existing_id = "capability_no_embedding_yet"
    graph = _ScriptedSingleTenantGraph(capability_rows=[[existing_id, "Legacy Capability", None]])
    incoming_id = "incoming_capability_beta"
    incoming_nodes = (_capability(incoming_id, "Brand New Capability"),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.canonical_id == incoming_id

    emitter.flush()
    entries = read_lines(log_path)
    warning_entries = [entry for entry in entries if entry.get("outcome") == "warning"]
    assert len(warning_entries) == 1
    assert warning_entries[0]["skipped_missing_embedding_count"] == 1


def test_no_warning_logged_when_nothing_is_skipped(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    existing_id = "capability_data_encryption"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Data Encryption Capability", [1.0, 0.0]]]
    )
    incoming_id = "incoming_capability_gamma"
    incoming_nodes = (_capability(incoming_id, "Data Encryption at Rest"),)

    resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        emitter=emitter,
    )

    emitter.flush()
    entries = read_lines(log_path)
    assert not any(entry.get("outcome") == "warning" for entry in entries)


def test_below_threshold_semantic_score_mints_new_and_records_near_miss() -> None:
    existing_id = "capability_unrelated"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Totally Unrelated Capability", [0.0, 1.0]]]
    )
    incoming_id = "incoming_capability_delta"
    incoming_nodes = (_capability(incoming_id, "Data Encryption at Rest"),)

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.canonical_id == incoming_id
    assert len(result.near_misses) == 1
    assert result.near_misses[0].nearest_existing_id == existing_id
