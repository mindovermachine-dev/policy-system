"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`: near-miss surfacing --
a below-threshold semantic score mints a new canonical node AND records a
`NearMissPair` carrying the actual similarity value that was computed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.errors import (
    CompanyMergeConfigurationError,
    UnknownReviewKindError,
)
from ps_service.company_merge.models import BaselineNode
from ps_service.company_merge.reviewable_kinds import (
    CAPABILITY,
    REVIEWABLE_KINDS,
    ReviewableKind,
    ReviewThresholds,
)
from ps_service.company_merge.similarity import cosine_similarity

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedSingleTenantGraph:
    """Satisfies `GraphHandle` structurally."""

    def __init__(
        self, *, capability_rows: list[object] | None = None, label: str = "Capability"
    ) -> None:
        self._capability_rows = capability_rows if capability_rows is not None else []
        self._label = label
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "MERGED_INTO" in q:
            return _FakeQueryResult([])
        if f"(n:{self._label}) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def test_dedupe_canonical_nodes_below_threshold_mints_and_records_near_miss(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    existing_id = "obl_existing_conduct_risk_assessment"
    incoming_id = "obl_incoming_report_incident"
    existing_text = "Conduct a risk assessment."
    incoming_text = "Report the incident to the authority."
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.0, 1.0]
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, existing_text, existing_vector]]
    )
    call_embedding = _ScriptedCallEmbedding({incoming_text: incoming_vector})
    incoming_nodes = (
        BaselineNode(id=incoming_id, properties={"name": incoming_text, "confidence": 0.9}),
    )

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind=CAPABILITY,
        single_tenant_graph=graph,
        model=_MODEL,
        thresholds=ReviewThresholds(merge=_THRESHOLD),
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.canonical_id == incoming_id

    expected_similarity = cosine_similarity(tuple(incoming_vector), tuple(existing_vector))
    assert expected_similarity < _THRESHOLD

    assert len(result.near_misses) == 1
    near_miss = result.near_misses[0]
    assert near_miss.incoming_id == incoming_id
    assert near_miss.incoming_text == incoming_text
    assert near_miss.nearest_existing_id == existing_id
    assert near_miss.nearest_existing_text == existing_text
    assert near_miss.similarity == expected_similarity


_REGISTERED_BUT_NOT_THIS_ONE = ReviewableKind(UNREGISTERED_KIND, "name", "global", "merge-above")


@pytest.mark.parametrize(
    ("kind", "thresholds", "instrument_id", "error", "names"),
    [
        pytest.param(
            _REGISTERED_BUT_NOT_THIS_ONE,
            ReviewThresholds(merge=_THRESHOLD),
            None,
            UnknownReviewKindError,
            UNREGISTERED_KIND,
            id="unregistered-kind",
        ),
        pytest.param(
            CAPABILITY,
            ReviewThresholds(),
            None,
            CompanyMergeConfigurationError,
            "Capability",
            id="merge-above-without-a-merge-threshold",
        ),
        pytest.param(
            CAPABILITY,
            ReviewThresholds(merge=_THRESHOLD, review=0.5),
            None,
            None,
            "",
            id="merge-above-with-a-review-floor-is-valid",
        ),
    ],
)
def test_dedupe_requires_registered_kind_and_thresholds_before_any_read(
    kind: ReviewableKind,
    thresholds: ReviewThresholds,
    instrument_id: str | None,
    error: type[Exception] | None,
    names: str,
) -> None:
    graph = _ScriptedSingleTenantGraph(label=kind.label)
    call_embedding = _ScriptedCallEmbedding({})
    nodes = (BaselineNode(id="cap_new", properties={"name": "New"}),)

    def run() -> object:
        return dedupe_canonical_nodes(
            nodes,
            kind=kind,
            single_tenant_graph=graph,
            model=_MODEL,
            thresholds=thresholds,
            instrument_id=instrument_id,
            call_embedding=call_embedding,
        )

    if error is None:
        run()
        return
    with pytest.raises(error, match=names):
        run()
    assert graph.calls == []
    assert call_embedding.calls == []


def test_a_review_only_kind_with_a_merge_threshold_fails_closed_before_any_read(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    graph = _ScriptedSingleTenantGraph(label=test_only_reviewable_kind.label)

    with pytest.raises(CompanyMergeConfigurationError, match=test_only_reviewable_kind.label):
        dedupe_canonical_nodes(
            (BaselineNode(id="p1", properties={"name": "One"}),),
            kind=test_only_reviewable_kind,
            single_tenant_graph=graph,
            model=_MODEL,
            thresholds=ReviewThresholds(merge=0.9, review=0.5),
            instrument_id="instrument_a",
        )

    assert graph.calls == []


def test_a_per_instrument_kind_without_an_instrument_id_fails_closed_naming_it_before_any_read(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    graph = _ScriptedSingleTenantGraph(label=test_only_reviewable_kind.label)
    call_embedding = _ScriptedCallEmbedding({})

    with pytest.raises(CompanyMergeConfigurationError, match=test_only_reviewable_kind.label):
        dedupe_canonical_nodes(
            (BaselineNode(id="p1", properties={"name": "One"}),),
            kind=test_only_reviewable_kind,
            single_tenant_graph=graph,
            model=_MODEL,
            thresholds=ReviewThresholds(review=0.5),
            call_embedding=call_embedding,
        )

    assert graph.calls == []
    assert call_embedding.calls == []


@pytest.mark.parametrize(("review_floor", "expected_pairs"), [(0.5, 1), (0.7, 0), (None, 1)])
def test_merge_above_kind_with_a_review_threshold_surfaces_only_pairs_at_or_above_it(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    review_floor: float | None,
    expected_pairs: int,
) -> None:
    """The incoming node scores 0.6 against its one neighbour: below the merge line either way."""
    emitter, _log_path = make_emitter()
    widget = ReviewableKind("Widget", "name", "global", "merge-above")
    monkeypatch.setitem(REVIEWABLE_KINDS, widget.label, widget)
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[["widget_old", "Old widget", [1.0, 0.0]]], label="Widget"
    )

    result = dedupe_canonical_nodes(
        (BaselineNode(id="widget_new", properties={"name": "New widget"}),),
        kind=widget,
        single_tenant_graph=graph,
        model=_MODEL,
        thresholds=ReviewThresholds(merge=_THRESHOLD, review=review_floor),
        call_embedding=_ScriptedCallEmbedding({"New widget": [0.6, 0.8]}),
        emitter=emitter,
    )

    assert [(r.incoming_id, r.match_kind) for r in result.resolutions] == [("widget_new", "new")]
    assert len(result.near_misses) == expected_pairs


def test_the_text_property_and_label_in_the_index_read_come_from_the_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = ReviewableKind("Widget", "title", "global", "merge-above")
    monkeypatch.setitem(REVIEWABLE_KINDS, widget.label, widget)
    graph = _ScriptedSingleTenantGraph(label="Widget")

    dedupe_canonical_nodes(
        (BaselineNode(id="widget_new", properties={"title": "A title"}),),
        kind=widget,
        single_tenant_graph=graph,
        model=_MODEL,
        thresholds=ReviewThresholds(merge=_THRESHOLD),
    )

    assert graph.calls == ["MATCH (n:Widget) RETURN n.id, n.title, n.embedding"]
