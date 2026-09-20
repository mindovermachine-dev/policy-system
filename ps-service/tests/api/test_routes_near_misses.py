"""HTTP tests for `GET /near-misses` and `POST /near-misses/{review_id}/resolve`
(issue #35, Slices 2-4, AC-BI-003/004/005/006/007/008/009).

Mirrors `test_routes_restorations.py`'s style: `TestClient` +
`app.dependency_overrides` supplying a fake `NearMissReviewDependencies`
bundle, so request handling is exercised without a real graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from fastapi.testclient import TestClient

from ps_service.api.dependencies import provide_near_miss_review_dependencies
from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
from ps_service.company_merge.errors import StalePendingReviewError
from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle


@dataclass
class _FakeGraphHandle:
    """Never actually queried by these fakes -- `list_pending_reviews` is scripted directly."""


def _record(
    review_id: str = "review_aaa",
    *,
    kind: Literal["Capability", "Policy"] = "Capability",
    similarity: float = 0.62,
) -> PendingReviewRecord:
    return PendingReviewRecord(
        id=review_id,
        kind=kind,
        incoming_id="capability_incoming_a",
        incoming_text="Report the incident to the authority.",
        nearest_existing_id="capability_existing_a",
        nearest_existing_text="Conduct a risk assessment.",
        similarity=similarity,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _unexpected_resolve_review(
    graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
) -> ResolveOutcome | None:
    """Default `resolve_review` stub for a list-only test -- fails loudly if ever called."""
    _ = graph, review_id, decision
    raise AssertionError("resolve_review should not be called in this test")


def _fake_dependencies(
    records: tuple[PendingReviewRecord, ...],
    *,
    resolve: Callable[[GraphHandle, str, Literal["keep-separate", "merge"]], ResolveOutcome | None]
    | None = None,
) -> tuple[NearMissReviewDependencies, list[ServiceConfig]]:
    open_calls: list[ServiceConfig] = []

    def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
        open_calls.append(config)
        return cast("GraphHandle", _FakeGraphHandle())

    def _list_pending_reviews(graph: GraphHandle) -> tuple[PendingReviewRecord, ...]:
        _ = graph
        return records

    dependencies = NearMissReviewDependencies(
        open_single_tenant_graph=_open_single_tenant_graph,
        list_pending_reviews=_list_pending_reviews,
        resolve_review=resolve or _unexpected_resolve_review,
    )
    return dependencies, open_calls


def _app_config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
    )


def _client_with_records(records: tuple[PendingReviewRecord, ...]) -> TestClient:
    dependencies, _ = _fake_dependencies(records)
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    return TestClient(app)


def test_get_near_misses_empty_graph_returns_empty_reviews_list() -> None:
    client = _client_with_records(())

    response = client.get("/near-misses")

    assert response.status_code == 200
    assert response.json() == {"reviews": []}


def test_get_near_misses_returns_id_incoming_text_existing_text_similarity() -> None:
    """AC-BI-003: every unresolved PendingReview is shown with id, texts, similarity."""
    record = _record()
    client = _client_with_records((record,))

    response = client.get("/near-misses")

    assert response.status_code == 200
    body = response.json()
    assert len(body["reviews"]) == 1
    entry = body["reviews"][0]
    assert entry["id"] == record.id
    assert entry["kind"] == record.kind
    assert entry["incoming_text"] == record.incoming_text
    assert entry["nearest_existing_text"] == record.nearest_existing_text
    assert entry["similarity"] == record.similarity
    # AC-BI-003 requires only id/incoming text/existing text/similarity -- the
    # underlying node ids are deliberately not part of the wire response.
    assert "incoming_id" not in entry
    assert "nearest_existing_id" not in entry


def test_get_near_misses_returns_every_unresolved_review_in_order() -> None:
    first = _record("review_aaa", kind="Capability", similarity=0.6)
    second = _record("review_bbb", kind="Policy", similarity=0.75)
    client = _client_with_records((first, second))

    response = client.get("/near-misses")

    assert response.status_code == 200
    ids = [entry["id"] for entry in response.json()["reviews"]]
    assert ids == ["review_aaa", "review_bbb"]


def test_get_near_misses_opens_the_single_tenant_graph_via_injected_config() -> None:
    dependencies, open_calls = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    client = TestClient(app)

    client.get("/near-misses")

    assert len(open_calls) == 1


def test_get_near_misses_is_unauthenticated_never_401_or_403() -> None:
    client = _client_with_records(())

    response = client.get("/near-misses")

    assert response.status_code not in (401, 403)


def _client_for_resolve(
    resolve: Callable[[GraphHandle, str, Literal["keep-separate", "merge"]], ResolveOutcome | None],
) -> TestClient:
    dependencies, _ = _fake_dependencies((), resolve=resolve)
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    return TestClient(app)


def test_post_resolve_keep_separate_returns_the_resolved_review() -> None:
    """AC-BI-004: `decision="keep-separate"` succeeds and echoes the resolved review id."""
    recorded_calls: list[tuple[str, str]] = []

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        recorded_calls.append((review_id, decision))
        return ResolveOutcome(review_id=review_id, decision=decision)

    client = _client_for_resolve(_resolve)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "keep-separate"})

    assert response.status_code == 200
    assert response.json() == {
        "review_id": "review_aaa",
        "decision": "keep-separate",
        "winner_id": None,
        "loser_id": None,
    }
    assert recorded_calls == [("review_aaa", "keep-separate")]


def test_post_resolve_unknown_id_returns_404_pending_review_not_found() -> None:
    """AC-BI-008 (not-found half): a bad id surfaces as a clear 404, not a 500/200."""

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, review_id, decision
        return None

    client = _client_for_resolve(_resolve)

    response = client.post(
        "/near-misses/review_missing/resolve", json={"decision": "keep-separate"}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "pending_review_not_found"
    assert "review_missing" in body["error"]["message"]


def test_post_resolve_rejects_unknown_decision_with_422() -> None:
    """Pydantic's `Literal["keep-separate", "merge"]` rejects any other value.

    `resolve_review` is never called: the default `_unexpected_resolve_review`
    stub would raise `AssertionError` if it were, and this test passes --
    proving validation rejects an unknown decision before the route body
    ever runs.
    """
    dependencies, _ = _fake_dependencies(())
    app = create_app(_app_config())
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    client = TestClient(app)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "not-a-decision"})

    assert response.status_code == 422


def test_post_resolve_merge_returns_winner_and_loser() -> None:
    """AC-BI-005/006/007: `decision="merge"` succeeds and echoes winner/loser ids."""
    recorded_calls: list[tuple[str, str]] = []

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        recorded_calls.append((review_id, decision))
        return ResolveOutcome(
            review_id=review_id,
            decision=decision,
            winner_id="capability_winner",
            loser_id="capability_loser",
        )

    client = _client_for_resolve(_resolve)

    response = client.post("/near-misses/review_aaa/resolve", json={"decision": "merge"})

    assert response.status_code == 200
    assert response.json() == {
        "review_id": "review_aaa",
        "decision": "merge",
        "winner_id": "capability_winner",
        "loser_id": "capability_loser",
    }
    assert recorded_calls == [("review_aaa", "merge")]


def test_post_resolve_merge_unknown_id_returns_404_pending_review_not_found() -> None:
    """AC-BI-008 (not-found half): a bad id surfaces as a clear 404 for merge too."""

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, review_id, decision
        return None

    client = _client_for_resolve(_resolve)

    response = client.post("/near-misses/review_missing/resolve", json={"decision": "merge"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "pending_review_not_found"
    assert "review_missing" in body["error"]["message"]


def test_post_resolve_merge_stale_reference_returns_404_with_stale_message() -> None:
    """AC-BI-008/CHANGES.md H2: a review referencing an already-deleted node is stale.

    `pending_review.resolve_review` raises `StalePendingReviewError`
    (company_merge, never `ps_service.api`) -- this route/orchestration
    layer translates it into the same `PendingReviewNotFoundError`
    (404, `pending_review_not_found`) with H2's dedicated message.
    """

    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph, decision
        raise StalePendingReviewError(review_id)

    client = _client_for_resolve(_resolve)

    response = client.post("/near-misses/review_stale/resolve", json={"decision": "merge"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "pending_review_not_found"
    assert "no longer exists" in body["error"]["message"]
    assert "stale" in body["error"]["message"]
