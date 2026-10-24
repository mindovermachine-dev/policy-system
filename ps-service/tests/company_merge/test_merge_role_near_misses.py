"""Ingest raises a Role near miss: `merge_baseline_graph` -> `PendingReview` -> `list-near-misses`.

The real `merge_baseline_graph` runs over a gateway rig whose baseline defines Roles with scripted
name embeddings. What persists is read back through the listing the MCP tool and REST route use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._gateway_fakes import FamilyABaseline, RigReadHandle, family_a_baseline_graph
from company_merge._per_instrument_rig import seed_defined_node
from company_merge._seed_merge_graphs import BaselineGraphReader
from company_merge._thresholds import REVIEW_THRESHOLD
from ps_service.api.near_miss_review_orchestration import (
    NearMissReviewDependencies,
    run_list_near_misses,
)
from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.company_merge.merge import MergeResult, merge_baseline_graph
from ps_service.company_merge.pending_review import (
    get_pending_review,
    list_pending_reviews,
    resolve_review,
)
from ps_service.config import ServiceConfig
from ps_service.llm_interface.errors import LlmProviderError

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig
    from company_merge._seed_merge_graphs import (
        _Result,  # pyright: ignore[reportPrivateUsage] -- the reader's own return type
    )

_INSTRUMENT_ID = "ROLES-1.0"
_MODEL = "fake-embed-model"
_MERGE = 0.9
_ROLE_FLOOR = 0.8
_AUTHORITY = "Market surveillance authority"
_AUTHORITIES = "Market surveillance authorities"
_IMPORTER = "Importer"
_VECTORS: dict[str, tuple[float, ...]] = {
    _AUTHORITY: (1.0, 0.0),
    _AUTHORITIES: (0.8, 0.6),  # cosine 0.8 against the authority
    _IMPORTER: (0.0, 1.0),  # cosine 0.0 against the authority
}


class _ByNameEmbedding:
    """An `EmbeddingCaller` answering each Role name with its scripted vector."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del timeout
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model,
            data=[Embedding(embedding=list(_VECTORS[inputs[0]]), index=0, object="embedding")],
        )


class _UnreachableProvider:
    """An `EmbeddingCaller` whose every call fails like an unreachable provider."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del model, inputs, timeout
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


class _RecordingReader(BaselineGraphReader):
    """A baseline reader that records every statement it is asked."""

    def __init__(self, graph: InMemoryGraph) -> None:
        super().__init__(graph)
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append(q)
        return super().query(q, params)


def _baseline(roles: dict[str, str]) -> FamilyABaseline:
    return FamilyABaseline(
        instrument_id=_INSTRUMENT_ID,
        instrument_properties={"id": _INSTRUMENT_ID, "title": "Roles"},
        roles=roles,
        defines=tuple((role_id, "Art. 1") for role_id in roles),
    )


_TWO_NEAR = {"role_authority": _AUTHORITY, "role_authorities": _AUTHORITIES}


def _merge(
    rig: MergeRig,
    emitter: object,
    baseline: FamilyABaseline,
    *,
    role_review: float | None = _ROLE_FLOOR,
    caller: object | None = None,
) -> MergeResult:
    return merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=rig.legacy,
        embed_model=_MODEL,
        merge_threshold=_MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=role_review,
        call_embedding=caller or _ByNameEmbedding(),  # pyright: ignore[reportArgumentType]
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        **rig.merge_keywords(),
    )


def _listing(rig: MergeRig) -> list[tuple[str, str, str, float]]:
    config = ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )
    dependencies = NearMissReviewDependencies(
        open_single_tenant_graph=lambda _config: RigReadHandle(rig.policy_system),
        list_pending_reviews=list_pending_reviews,
        resolve_review=resolve_review,
        get_pending_review=get_pending_review,
    )
    response = run_list_near_misses(config=config, dependencies=dependencies)
    return [
        (r.kind, r.incoming_text, r.nearest_existing_text, r.similarity) for r in response.reviews
    ]


def _review_nodes(rig: MergeRig) -> list[dict[str, object]]:
    return [
        props for (label, _), props in rig.policy_system.nodes.items() if label == "PendingReview"
    ]


def test_two_near_roles_ingested_through_merge_are_listed_as_one_role_near_miss(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    result = _merge(merge_rig, emitter, _baseline(_TWO_NEAR))

    assert _listing(merge_rig) == [("Role", _AUTHORITIES, _AUTHORITY, pytest.approx(0.8))]
    assert result.pending_review_count == 1
    assert result.role_near_miss_count == 1
    assert result.new_role_count == 2


def test_a_second_run_over_the_same_baseline_raises_no_second_review(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    _merge(merge_rig, emitter, _baseline(_TWO_NEAR))

    second = _merge(merge_rig, emitter, _baseline(_TWO_NEAR))

    assert len(_listing(merge_rig)) == 1
    assert len(_review_nodes(merge_rig)) == 1
    assert second.pending_review_count == 0
    assert second.role_near_miss_count == 0
    assert second.new_role_count == 0


def test_both_roles_are_written_and_neither_is_merged(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    _merge(merge_rig, emitter, _baseline(_TWO_NEAR))

    assert ("Role", "role_authority") in merge_rig.policy_system.nodes
    assert ("Role", "role_authorities") in merge_rig.policy_system.nodes
    assert not [key for key in merge_rig.policy_system.nodes if key[0] == "MergedRole"]


def test_roles_below_the_threshold_raise_no_review(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    result = _merge(
        merge_rig, emitter, _baseline({"role_authority": _AUTHORITY, "role_importer": _IMPORTER})
    )

    assert _listing(merge_rig) == []
    assert result.role_near_miss_count == 0
    assert result.new_role_count == 2


def test_a_role_pair_spanning_two_instruments_raises_no_review(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_defined_node(
        merge_rig,
        label="Role",
        node_id="role_other",
        name=_AUTHORITIES,
        instrument_id="OTHER-1.0",
        embedding=_VECTORS[_AUTHORITIES],
    )

    result = _merge(merge_rig, emitter, _baseline({"role_authority": _AUTHORITY}))

    assert _listing(merge_rig) == []
    assert result.role_near_miss_count == 0


def test_a_missing_role_threshold_raises_before_any_graph_call(merge_rig: MergeRig) -> None:
    baseline = _RecordingReader(family_a_baseline_graph(_baseline(_TWO_NEAR)))
    single_tenant = RigReadHandle(merge_rig.policy_system)

    with pytest.raises(
        CompanyMergeConfigurationError, match="PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD"
    ):
        merge_baseline_graph(
            _INSTRUMENT_ID,
            baseline_graph=baseline,
            single_tenant_graph=single_tenant,
            embed_model=_MODEL,
            merge_threshold=_MERGE,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=None,
            **merge_rig.merge_keywords(),
        )

    assert baseline.calls == []
    assert merge_rig.groups == []


def test_an_embedding_failure_in_role_detection_aborts_before_any_write(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    with pytest.raises(LlmProviderError):
        _merge(merge_rig, emitter, _baseline(_TWO_NEAR), caller=_UnreachableProvider())

    assert merge_rig.groups == []
    assert _review_nodes(merge_rig) == []


def test_a_role_pair_reviewed_before_an_amendment_is_not_reviewed_again(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    _merge(merge_rig, emitter, _baseline(_TWO_NEAR))

    # the amended baseline keeps the pair and adds an unrelated Role
    amended = _baseline({**_TWO_NEAR, "role_importer": _IMPORTER})
    result = _merge(merge_rig, emitter, amended)

    assert len(_listing(merge_rig)) == 1
    assert result.role_near_miss_count == 0
    assert result.new_role_count == 1


def test_a_new_role_pairs_with_a_role_the_instrument_already_defines(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    """An amendment that mints a new spelling of an old Role is reviewed against the old one."""
    emitter, _ = make_emitter()
    _merge(merge_rig, emitter, _baseline({"role_authority": _AUTHORITY}))

    amended = _baseline({"role_authority": _AUTHORITY, "role_authorities": _AUTHORITIES})
    result = _merge(merge_rig, emitter, amended)

    assert _listing(merge_rig) == [("Role", _AUTHORITIES, _AUTHORITY, pytest.approx(0.8))]
    assert result.role_near_miss_count == 1
