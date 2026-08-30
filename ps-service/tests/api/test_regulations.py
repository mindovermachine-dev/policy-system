"""HTTP tests for `GET /regulations` (the curated-catalog endpoint)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.api.catalog import REGULATION_CATALOG

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_regulations_returns_curated_catalog_with_celex_and_title(client: TestClient) -> None:
    """AC-BI-001: the response lists every curated regulation as `{celex, title}`."""
    response = client.get("/regulations")

    assert response.status_code == 200
    body = response.json()
    returned = {(item["celex"], item["title"]) for item in body["regulations"]}
    expected = {(entry.celex, entry.title) for entry in REGULATION_CATALOG}
    assert returned == expected
    assert all(set(item) == {"celex", "title"} for item in body["regulations"])


def test_get_regulations_response_carries_a_run_id(client: TestClient) -> None:
    """AC-BI-010: the success body carries the request-scoped run id."""
    response = client.get("/regulations")

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert isinstance(run_id, str)
    assert run_id


def test_get_regulations_is_unauthenticated_never_401_or_403(client: TestClient) -> None:
    """AC-BI-012: the endpoint is unauthenticated — an anonymous GET never 401/403s."""
    response = client.get("/regulations")

    assert response.status_code not in (401, 403)
