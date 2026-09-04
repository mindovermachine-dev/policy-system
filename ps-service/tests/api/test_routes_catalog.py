"""HTTP tests for `GET /catalog` (the full curated-instrument listing, AC-BI-011)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.api.catalog import CATALOG

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_get_catalog_returns_every_curated_entry_unfiltered(client: TestClient) -> None:
    """AC-BI-011: every curated entry -- external and internal, no celex filter."""
    response = client.get("/catalog")

    assert response.status_code == 200
    body = response.json()
    returned_ids = {item["instrument_id"] for item in body["instruments"]}
    expected_ids = {entry.instrument_id for entry in CATALOG}
    assert returned_ids == expected_ids
    assert len(body["instruments"]) == len(CATALOG)


def test_get_catalog_entries_carry_the_documented_fields(client: TestClient) -> None:
    """Every entry carries exactly ``instrument_id``/``title``/``source_type``/``jurisdiction``."""
    response = client.get("/catalog")

    body = response.json()
    for item in body["instruments"]:
        assert set(item) == {"instrument_id", "title", "source_type", "jurisdiction"}
        assert item["source_type"] in ("external", "internal")


def test_get_catalog_is_unauthenticated_never_401_or_403(client: TestClient) -> None:
    """AC-BI-011 companion: an anonymous GET never 401/403s (mirrors GET /regulations)."""
    response = client.get("/catalog")

    assert response.status_code not in (401, 403)


def test_get_catalog_succeeds_with_no_falkordb_or_llm_fixture_wired(client: TestClient) -> None:
    """AC-BI-011: no FalkorDB/LLM dependency -- the shared ``client`` fixture wires neither
    (``tests/api/conftest.py``'s bare ``TestClient``, ``lifespan`` never entered, so no
    dependency probe of any kind has run) and the call still succeeds.
    """
    response = client.get("/catalog")

    assert response.status_code == 200
