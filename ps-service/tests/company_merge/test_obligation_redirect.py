"""Company Merge follows `MergedObligation` markers (issue #190, H1 / CHANGES.md A7, AC-BI-007).

A cleanup obligation merge deletes the absorbed Obligation but leaves a
`MergedObligation {id, merged_into}` marker. A later merge whose baseline regenerates the
absorbed Obligation drops it before any persist and rewires its `HAS` / `SATISFIED_BY` /
`REQUIRES` edges onto the (terminal) survivor, so the duplicate never returns.

Fakes are hand-written and satisfy `GraphHandle` structurally; each test module carries its
own copies (package convention).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BareEdge, BaselineGraph, BaselineNode
from ps_service.company_merge.obligation_redirect import (
    apply_obligation_redirects,
    follow_obligation_redirect,
    read_obligation_redirects,
    resolve_obligation_redirects,
)
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.logging import configure

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

_THRESHOLD = 0.85
_ROLE = "role_h_operator"
_ABSORBED = obligation_id(_ROLE, "Report incidents promptly.")
_SURVIVOR = obligation_id(_ROLE, "Report incidents")
_OTHER = obligation_id(_ROLE, "Keep records.")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _RegulatoryInstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _SingleTenant:
    """Single-tenant graph with scripted `MergedObligation` markers and Obligation ids."""

    def __init__(
        self, *, markers: dict[str, str] | None = None, existing: frozenset[str] = frozenset()
    ) -> None:
        self._markers = markers or {}
        self._existing = existing
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, params))
        if "MergedObligation" in q:
            return _Result([[k, v] for k, v in self._markers.items()])
        if "MATCH (o:Obligation) WHERE o.id IN $ids" in q:
            assert params is not None
            ids = cast("list[str]", params["ids"])
            return _Result([[i] for i in ids if i in self._existing])
        return _Result([])

    def matching(self, fragment: str) -> list[tuple[str, dict[str, object] | None]]:
        return [c for c in self.calls if fragment in c[0]]


class _Baseline:
    """One Role with a regenerated absorbed Obligation and one unrelated Obligation."""

    def __init__(self, cap_id: str) -> None:
        self._cap_id = cap_id

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        if "[:HAS]" in q:
            return _Result([[_ROLE, _ABSORBED], [_ROLE, _OTHER]])
        if "[:SATISFIED_BY]" in q:
            return _Result([["REG-H_req_art_1.1", _ABSORBED]])
        if "[:REQUIRES]" in q:
            return _Result([[_ABSORBED, self._cap_id]])
        if any(
            f"(n:{label})" in q
            for label in ("Policy", "Standard", "Control", "PracticeArea", "RiskPath")
        ):
            return _Result([])
        if "n.description" in q:
            return _Result([[self._cap_id, "Incident Reporting", 0.8, None, None]])
        if "(n:Obligation) RETURN" in q:
            return _Result(
                [[_ABSORBED, "Report incidents promptly.", 0.9], [_OTHER, "Keep records.", 0.9]]
            )
        if "n.role_id" in q:
            return _Result([["REG-H_req_art_1.1", "Must report.", "requirement", 0.9, _ROLE]])
        if "n.name, n.confidence" in q:
            return _Result([[_ROLE, "Operator", 0.9]])
        if "[e:DEFINES]" in q:
            return _Result([[_ROLE, "Article 1(1)"]])
        if "[e:EXPRESSES]" in q:
            return _Result([["REG-H_req_art_1.1", "Article 1(1)"]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_RegulatoryInstrumentNode({"id": "REG-H", "title": "Test"})]])
        return _Result([])


def _node(node_id: str, text: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": text, "confidence": 0.9})


def _baseline_graph(*obligations: BaselineNode, edges: tuple[BareEdge, ...] = ()) -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id="REG-H",
        regulatory_instrument_properties={"id": "REG-H"},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=obligations,
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=edges,
    )


def test_read_obligation_redirects_is_one_literal_read_returning_the_marker_map() -> None:
    graph = _SingleTenant(markers={"obl_a": "obl_s", "obl_b": "obl_s"})

    redirects = read_obligation_redirects(graph)

    assert redirects == {"obl_a": "obl_s", "obl_b": "obl_s"}
    assert [q for q, _ in graph.calls] == ["MATCH (m:MergedObligation) RETURN m.id, m.merged_into"]


def test_follow_returns_an_id_with_no_marker_unchanged() -> None:
    assert follow_obligation_redirect("obl_x", {}) == "obl_x"


def test_follow_goes_to_the_terminal_survivor_through_a_chain() -> None:
    assert follow_obligation_redirect("a", {"a": "b", "b": "c"}) == "c"


def test_follow_rejects_a_cycle() -> None:
    with pytest.raises(CompanyMergeValidationError):
        follow_obligation_redirect("a", {"a": "b", "b": "a"})


def test_apply_drops_the_absorbed_node_and_maps_it_to_the_terminal_survivor() -> None:
    baseline = _baseline_graph(_node(_ABSORBED, "x"), _node(_OTHER, "y"))

    rewritten, mapping = apply_obligation_redirects(
        baseline, {_ABSORBED: _SURVIVOR}, existing_ids=frozenset({_SURVIVOR})
    )

    assert [n.id for n in rewritten.obligation_nodes] == [_OTHER]
    assert mapping == {_ABSORBED: _SURVIVOR}


def test_apply_with_no_matching_marker_returns_the_baseline_unchanged() -> None:
    baseline = _baseline_graph(_node(_OTHER, "y"))

    rewritten, mapping = apply_obligation_redirects(
        baseline, {"unrelated": "elsewhere"}, existing_ids=frozenset()
    )

    assert rewritten is baseline
    assert mapping == {}


def test_apply_accepts_a_terminal_that_is_itself_in_the_baseline() -> None:
    baseline = _baseline_graph(_node(_ABSORBED, "x"), _node(_SURVIVOR, "y"))

    rewritten, mapping = apply_obligation_redirects(
        baseline, {_ABSORBED: _SURVIVOR}, existing_ids=frozenset()
    )

    assert [n.id for n in rewritten.obligation_nodes] == [_SURVIVOR]
    assert mapping == {_ABSORBED: _SURVIVOR}


def test_apply_fails_closed_when_the_terminal_exists_nowhere() -> None:
    baseline = _baseline_graph(_node(_ABSORBED, "x"))

    with pytest.raises(CompanyMergeValidationError) as excinfo:
        apply_obligation_redirects(baseline, {_ABSORBED: _SURVIVOR}, existing_ids=frozenset())

    assert _SURVIVOR in str(excinfo.value)


def test_resolve_issues_only_the_marker_read_when_no_baseline_obligation_is_absorbed() -> None:
    graph = _SingleTenant(markers={"unrelated": "elsewhere"})
    baseline = _baseline_graph(_node(_OTHER, "y"))

    rewritten, mapping = resolve_obligation_redirects(graph, baseline)

    assert rewritten is baseline
    assert mapping == {}
    assert len(graph.calls) == 1


def test_resolve_checks_that_the_terminal_survivor_exists_before_dropping_the_node() -> None:
    graph = _SingleTenant(markers={_ABSORBED: _SURVIVOR}, existing=frozenset({_SURVIVOR}))
    baseline = _baseline_graph(_node(_ABSORBED, "x"), _node(_OTHER, "y"))

    rewritten, mapping = resolve_obligation_redirects(graph, baseline)

    assert [n.id for n in rewritten.obligation_nodes] == [_OTHER]
    assert mapping == {_ABSORBED: _SURVIVOR}
    [(_, params)] = graph.matching("o.id IN $ids")
    assert params == {"ids": [_SURVIVOR]}


def test_resolve_with_a_deleted_survivor_raises_before_any_write() -> None:
    graph = _SingleTenant(markers={_ABSORBED: _SURVIVOR}, existing=frozenset())
    baseline = _baseline_graph(_node(_ABSORBED, "x"))

    with pytest.raises(CompanyMergeValidationError):
        resolve_obligation_redirects(graph, baseline)

    assert all("MERGE" not in q for q, _ in graph.calls)


def test_merge_baseline_graph_rewires_the_regenerated_obligation_onto_the_survivor(
    make_emitter: MakeEmitter,
) -> None:
    """A regenerated absorbed Obligation is never minted; its edges land on the survivor."""
    emitter, _log_path = make_emitter()
    cap_id = capability_id("Incident Reporting")
    single_tenant = _SingleTenant(markers={_ABSORBED: _SURVIVOR}, existing=frozenset({_SURVIVOR}))

    result = merge_baseline_graph(
        "REG-H",
        baseline_graph=_Baseline(cap_id),
        single_tenant_graph=single_tenant,
        embed_model="fake-embed-model",
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    mints = single_tenant.matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert [p["id"] for _, p in mints if p is not None] == [_OTHER]
    assert result.obligation_ids == (_OTHER,)
    has_writes = [p for _, p in single_tenant.matching("[:HAS]") if p is not None]
    assert {"source_id": _ROLE, "target_id": _SURVIVOR} in has_writes
    sat_writes = [p for _, p in single_tenant.matching("[:SATISFIED_BY]") if p is not None]
    assert sat_writes == [{"source_id": "REG-H_req_art_1.1", "target_id": _SURVIVOR}]
    req_writes = [p for _, p in single_tenant.matching("[:REQUIRES]") if p is not None]
    assert req_writes == [{"source_id": _SURVIVOR, "target_id": cap_id}]
    written = [p for q, p in single_tenant.calls if p is not None and "MERGE" in q]
    assert not any(_ABSORBED in map(str, p.values()) for p in written)


def test_merge_baseline_graph_without_markers_is_unchanged(make_emitter: MakeEmitter) -> None:
    emitter, _log_path = make_emitter()
    cap_id = capability_id("Incident Reporting")
    single_tenant = _SingleTenant()

    result = merge_baseline_graph(
        "REG-H",
        baseline_graph=_Baseline(cap_id),
        single_tenant_graph=single_tenant,
        embed_model="fake-embed-model",
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result.obligation_ids == (_ABSORBED, _OTHER)
    mints = single_tenant.matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert len(mints) == 2


def test_applying_a_redirect_emits_one_semantic_log_entry_with_the_count(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    graph = _SingleTenant(markers={_ABSORBED: _SURVIVOR}, existing=frozenset({_SURVIVOR}))
    baseline = _baseline_graph(_node(_ABSORBED, "x"), _node(_OTHER, "y"))

    resolve_obligation_redirects(graph, baseline, emitter=emitter)
    emitter.flush()

    [entry] = [e for e in read_lines(log_path) if e.get("action") == "redirect_merged_obligations"]
    assert entry["component"] == "company_merge"
    assert entry["outcome"] == "succeeded"
    assert entry["redirected_count"] == 1


def test_no_log_entry_is_emitted_when_nothing_is_redirected(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    resolve_obligation_redirects(
        _SingleTenant(), _baseline_graph(_node(_OTHER, "y")), emitter=emitter
    )
    emitter.flush()

    assert [
        e for e in read_lines(log_path) if e.get("action") == "redirect_merged_obligations"
    ] == []
