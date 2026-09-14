"""Tests for `ps_service.company_merge.dedup` (PLAN_REVIEWED.md §10
Increments 6-7): `read_existing_canonical_index` and `resolve_exact_match`.

Since issue #42 Company Merge dedupes Capability only -- Obligation is
Role-scoped and passed through, not deduped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

from ps_service.company_merge.dedup import (
    dedupe_canonical_nodes,
    read_existing_canonical_index,
    resolve_exact_match,
)
from ps_service.company_merge.models import BaselineNode, ExistingCanonicalNode

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedFakeGraph:
    """Satisfies `GraphHandle` structurally. Dispatches by a distinctive
    substring of the query text, mirroring
    `tests/company_merge/test_graph_reader.py`'s own `_ScriptedFakeGraph`
    dispatch style.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def test_read_existing_canonical_index_returns_capability_nodes_with_and_without_embedding() -> (
    None
):
    graph = _ScriptedFakeGraph(
        capability_rows=[
            ["capability_1", "Risk Assessment Capability", [0.7, 0.8]],
            ["capability_2", "Incident Reporting Capability", [0.9, 1.0]],
            ["capability_3", "Vulnerability Handling Capability", None],
        ],
    )

    result = read_existing_canonical_index(graph, "Capability")

    assert result == (
        ExistingCanonicalNode(
            id="capability_1", text="Risk Assessment Capability", embedding=(0.7, 0.8)
        ),
        ExistingCanonicalNode(
            id="capability_2", text="Incident Reporting Capability", embedding=(0.9, 1.0)
        ),
        ExistingCanonicalNode(
            id="capability_3", text="Vulnerability Handling Capability", embedding=None
        ),
    )
    assert result[2].embedding is None


def test_read_existing_canonical_index_returns_empty_tuple_for_empty_graph() -> None:
    graph = _ScriptedFakeGraph(capability_rows=[])

    result = read_existing_canonical_index(graph, "Capability")

    assert result == ()


def test_resolve_exact_match_returns_true_when_incoming_id_is_present() -> None:
    assert resolve_exact_match("cap_1", frozenset({"cap_1", "cap_2"})) is True


def test_resolve_exact_match_returns_false_when_incoming_id_is_absent() -> None:
    assert resolve_exact_match("cap_3", frozenset({"cap_1", "cap_2"})) is False


def test_resolve_exact_match_returns_false_for_empty_existing_ids() -> None:
    assert resolve_exact_match("cap_1", frozenset()) is False


def test_dedupe_canonical_nodes_second_same_run_mint_resolves_via_exact_match(
    make_emitter: MakeEmitter,
) -> None:
    """AC-TE-001 (live, end-to-end, issue #30): `resolve_exact_match`'s
    candidate pool is the FULL, growing `working_index` -- never restricted
    to `original_existing_ids` -- so two incoming nodes sharing the
    identical id still collapse via exact-key match regardless of same-run
    timing, unaffected by the semantic-match eligibility restriction this
    issue adds. The first occurrence mints (`match_kind="new"`); the second
    (same id) resolves `match_kind="exact"` onto it, never reaching
    `find_best_semantic_match` at all.
    """
    emitter, _log_path = make_emitter()
    shared_id = "cap_shared_between_two_incoming_nodes"
    graph = _ScriptedFakeGraph(capability_rows=[])
    incoming_nodes = (
        BaselineNode(id=shared_id, properties={"name": "First occurrence.", "confidence": 0.9}),
        BaselineNode(id=shared_id, properties={"name": "Second occurrence.", "confidence": 0.9}),
    )

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert len(result.resolutions) == 2
    first_resolution, second_resolution = result.resolutions
    assert first_resolution.incoming_id == shared_id
    assert first_resolution.match_kind == "new"
    assert first_resolution.canonical_id == shared_id
    assert second_resolution.incoming_id == shared_id
    assert second_resolution.match_kind == "exact"
    assert second_resolution.canonical_id == shared_id
