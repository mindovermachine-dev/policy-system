"""Tests for `ps_service.company_merge.dedup.resolve_capability_convergence_offline`
(PLAN.md Slice 2, issue #30): offline candidate-pool eligibility (AC-BI-003).

Mirrors `test_dedup_combined_resolution.py`'s
`test_dedupe_canonical_nodes_same_run_mints_do_not_converge_but_record_near_miss`
and `test_dedupe_canonical_nodes_records_near_miss_for_higher_scoring_mint_on_merge`
shapes exactly, adapted to the offline function's artifact-supplied-embedding
signature: the offline semantic-match candidate pool is restricted to
PRE-RUN existing nodes only (`original_existing_ids`), so a same-run mint --
however high its score -- can never itself be an eligible merge target
(AC-BI-003, the offline twin of the live path's AC-BI-001). A same-run mint
that would otherwise have won is instead recorded as a `NearMissPair`
(AC-BI-004, offline twin), whether the excluded mint is discovered on its
own (mint branch) or alongside an eligible pre-run merge target scoring
lower (merge branch).

This file previously asserted the OPPOSITE for two same-run mints (in-batch
convergence happens) -- per BASELINE.md's own note, that assertion became
wrong under issue #30's restriction and its update below is not a
regression.
"""

from __future__ import annotations

from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.models import BaselineNode
from ps_service.company_merge.similarity import cosine_similarity

_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedSingleTenantGraph:
    """Satisfies `GraphHandle` structurally -- an empty target index, so
    neither incoming node has any pre-existing match.
    """

    def __init__(self, *, capability_rows: list[object]) -> None:
        self._capability_rows = capability_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def _capability(node_id: str, name: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": name})


def test_offline_convergence_same_run_mints_do_not_converge_but_record_near_miss() -> None:
    """(AC-BI-002/AC-BI-004, offline twin of the live path's
    `test_dedupe_canonical_nodes_same_run_mints_do_not_converge_but_record_near_miss`):
    two incoming nodes with no PRE-RUN existing match but semantically
    equivalent to EACH OTHER (identical artifact-supplied embeddings) no
    longer converge onto one another. Both resolve `match_kind="new"`, and
    the excluded same-run mint is recorded as a `NearMissPair` instead (the
    two vectors are identical, so cosine similarity is exactly 1.0, at/above
    `_THRESHOLD`).
    """
    first_id = "incoming_capability_first_alpha"
    second_id = "incoming_capability_second_beta"
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    incoming_nodes = (
        _capability(first_id, "Data Encryption at Rest"),
        _capability(second_id, "Encrypt Data While Stored"),
    )

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={first_id: (1.0, 0.0), second_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    assert len(result.resolutions) == 2
    first_resolution = next(r for r in result.resolutions if r.incoming_id == first_id)
    second_resolution = next(r for r in result.resolutions if r.incoming_id == second_id)
    assert first_resolution.match_kind == "new"
    assert first_resolution.canonical_id == first_id
    assert second_resolution.match_kind == "new"
    assert second_resolution.canonical_id == second_id

    assert len(result.near_misses) == 1
    near_miss = result.near_misses[0]
    assert near_miss.incoming_id == second_id
    assert near_miss.nearest_existing_id == first_id
    assert near_miss.similarity == 1.0


def test_offline_convergence_records_near_miss_for_higher_scoring_mint_on_merge() -> None:
    """CHANGES.md row 1 (HIGH), offline twin of the live path's
    `test_dedupe_canonical_nodes_records_near_miss_for_higher_scoring_mint_on_merge`
    (Appendix B): one pre-run existing Capability `P`; incoming `M` mints
    first (scores below threshold against `P`); incoming `X` scores ABOVE
    threshold against BOTH `P` (eligible -- the merge target) and `M`
    (same-run mint, and a HIGHER score than `P`). `X` must still merge onto
    `P` (the eligible best), but the higher-scoring excluded mint `M` must
    ALSO be recorded as a near-miss -- proving the merge branch's AC-BI-004
    check fires independently of the mint branch's pre-existing one, on the
    offline path too.
    """
    existing_id = "capability_existing_p"
    mint_id = "incoming_capability_m"
    merge_incoming_id = "incoming_capability_x"
    p_vector = (1.0, 0.0)
    m_vector = (0.766, 0.643)
    x_vector = (0.8998, 0.4376)
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "P Capability", list(p_vector)]]
    )
    incoming_nodes = (
        _capability(mint_id, "M Capability"),
        _capability(merge_incoming_id, "X Capability"),
    )

    similarity_x_p = cosine_similarity(x_vector, p_vector)
    similarity_x_m = cosine_similarity(x_vector, m_vector)
    assert similarity_x_p >= _THRESHOLD
    assert similarity_x_m >= _THRESHOLD
    assert similarity_x_m > similarity_x_p

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={mint_id: m_vector, merge_incoming_id: x_vector},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
    )

    mint_resolution = next(r for r in result.resolutions if r.incoming_id == mint_id)
    merge_resolution = next(r for r in result.resolutions if r.incoming_id == merge_incoming_id)
    assert mint_resolution.match_kind == "new"
    assert mint_resolution.canonical_id == mint_id
    # X merges onto the ELIGIBLE best (the pre-run existing node P), not the
    # higher-scoring same-run mint M.
    assert merge_resolution.match_kind == "semantic"
    assert merge_resolution.canonical_id == existing_id

    x_near_miss = next(nm for nm in result.near_misses if nm.incoming_id == merge_incoming_id)
    assert x_near_miss.nearest_existing_id == mint_id
    assert x_near_miss.similarity == similarity_x_m
