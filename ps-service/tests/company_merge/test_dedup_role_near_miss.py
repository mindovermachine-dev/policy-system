"""Role near-miss detection: per-instrument scope, review-only rule, live path.

A Role is compared by name embedding against the Roles its instrument defines and against the
run's own mints. A close pair is cited and never merged, so every Role resolves as `new`. The rig
is the in-memory gateway; incoming vectors are cached on the nodes so no embedding call is needed
unless a test wants one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._per_instrument_rig import INSTRUMENT_A, INSTRUMENT_B, seed_defined_node
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.models import BaselineNode, DedupResult
from ps_service.company_merge.reviewable_kinds import (
    ROLE,
    ReviewThresholds,
    require_thresholds,
    reviewable_kind,
)
from ps_service.llm_interface.errors import LlmProviderError

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig

_MODEL = "fake-embed-model"
_REVIEW = ReviewThresholds(review=0.8)
_ANCHOR = (1.0, 0.0)
_NEAR = (0.8, 0.6)  # cosine 0.8 against (1, 0)
_FAR = (0.0, 1.0)  # cosine 0.0 against (1, 0)


class _FailingCaller:
    """An `EmbeddingCaller` whose every call fails like an unreachable provider."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del model, inputs, timeout
        self.calls += 1
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


class _UnusedCaller:
    """An `EmbeddingCaller` that must not be needed."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del inputs, timeout
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=[1.0, 0.0], index=0, object="embedding")]
        )


def _role(role_id: str, vector: tuple[float, ...] | None) -> BaselineNode:
    return BaselineNode(id=role_id, properties={"name": f"name {role_id}"}, embedding=vector)


def _dedup(
    rig: MergeRig,
    nodes: tuple[BaselineNode, ...],
    *,
    instrument_id: str = INSTRUMENT_A,
    thresholds: ReviewThresholds = _REVIEW,
) -> DedupResult:
    return dedupe_canonical_nodes(
        nodes,
        kind=ROLE,
        single_tenant_graph=rig.legacy,
        model=_MODEL,
        thresholds=thresholds,
        instrument_id=instrument_id,
        call_embedding=_UnusedCaller(),
    )


def test_role_is_registered_as_a_per_instrument_review_only_kind() -> None:
    assert reviewable_kind("Role") is ROLE
    assert (ROLE.label, ROLE.text_property) == ("Role", "name")
    assert (ROLE.scope, ROLE.rule) == ("per-instrument", "review-only")


def test_two_near_roles_of_one_instrument_raise_one_pair_and_neither_merges(
    merge_rig: MergeRig,
) -> None:
    result = _dedup(merge_rig, (_role("r_1", _ANCHOR), _role("r_2", _NEAR)))

    assert [(m.incoming_id, m.nearest_existing_id) for m in result.near_misses] == [("r_2", "r_1")]
    assert [(r.match_kind, r.canonical_id) for r in result.resolutions] == [
        ("new", "r_1"),
        ("new", "r_2"),
    ]


def test_a_pair_below_the_role_threshold_raises_nothing(merge_rig: MergeRig) -> None:
    result = _dedup(merge_rig, (_role("r_1", _ANCHOR), _role("r_2", _FAR)))

    assert result.near_misses == ()
    assert {r.match_kind for r in result.resolutions} == {"new"}


@pytest.mark.parametrize(("review", "expected"), [(0.8, 1), (0.81, 0)])
def test_the_role_threshold_is_inclusive(merge_rig: MergeRig, review: float, expected: int) -> None:
    result = _dedup(
        merge_rig,
        (_role("r_1", _ANCHOR), _role("r_2", _NEAR)),
        thresholds=ReviewThresholds(review=review),
    )

    assert len(result.near_misses) == expected


def test_a_new_role_pairs_with_a_role_the_instrument_already_defines(merge_rig: MergeRig) -> None:
    seed_defined_node(
        merge_rig,
        label="Role",
        node_id="r_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=_ANCHOR,
    )

    result = _dedup(merge_rig, (_role("r_new", _NEAR),))

    assert [(m.incoming_id, m.nearest_existing_id) for m in result.near_misses] == [
        ("r_new", "r_old")
    ]


def test_mutually_near_roles_yield_one_pair_per_incoming_role_never_a_clique(
    merge_rig: MergeRig,
) -> None:
    forms = tuple(_role(f"r_{index}", _ANCHOR) for index in range(8))

    result = _dedup(merge_rig, forms)

    assert len(result.near_misses) <= len(forms) - 1
    incoming_ids = [m.incoming_id for m in result.near_misses]
    assert len(incoming_ids) == len(set(incoming_ids))
    assert {r.match_kind for r in result.resolutions} == {"new"}


def test_roles_defined_by_different_instruments_raise_no_pair(merge_rig: MergeRig) -> None:
    seed_defined_node(  # no cached embedding: scoring it would need a call
        merge_rig, label="Role", node_id="r_other", name="other", instrument_id=INSTRUMENT_B
    )
    seed_defined_node(
        merge_rig,
        label="Role",
        node_id="r_other_cached",
        name="other cached",
        instrument_id=INSTRUMENT_B,
        embedding=_ANCHOR,
    )

    result = _dedup(merge_rig, (_role("r_mine", _ANCHOR),))

    assert result.near_misses == ()
    assert [r.match_kind for r in result.resolutions] == ["new"]


def test_an_embedding_failure_propagates_and_nothing_is_written(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_defined_node(
        merge_rig,
        label="Role",
        node_id="r_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=_ANCHOR,
    )
    caller = _FailingCaller()

    with pytest.raises(LlmProviderError):
        dedupe_canonical_nodes(
            (_role("r_new", None),),
            kind=ROLE,
            single_tenant_graph=merge_rig.legacy,
            model=_MODEL,
            thresholds=_REVIEW,
            instrument_id=INSTRUMENT_A,
            call_embedding=caller,
            emitter=emitter,
        )

    assert caller.calls >= 1
    assert merge_rig.groups == []


def test_role_thresholds_refuse_a_merge_threshold() -> None:
    with pytest.raises(CompanyMergeConfigurationError, match="Role"):
        require_thresholds(ROLE, ReviewThresholds(merge=0.5, review=0.8))


def test_role_thresholds_require_a_review_threshold() -> None:
    with pytest.raises(CompanyMergeConfigurationError, match="Role"):
        require_thresholds(ROLE, ReviewThresholds())
