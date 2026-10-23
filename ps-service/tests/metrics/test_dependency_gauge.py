"""The aggregate unhealthy-dependency gauge: two series, no names, a pure read of the registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from metrics._app import auth_enabled_config
from metrics._logs import metrics_entries
from metrics._scrape import samples
from ps_service import dependency_health
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    LLM_INTERFACE,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    mark_unhealthy,
)
from ps_service.main import (
    _GATING_DEPENDENCIES,  # pyright: ignore[reportPrivateUsage]  # the split the gauge must follow
    _READY_DEPENDENCIES,  # pyright: ignore[reportPrivateUsage]  # the list /ready reports and the gauge counts
    create_app,
)
from ps_service.metrics import build_metrics

if TYPE_CHECKING:
    from pathlib import Path

_GAUGE = "ps_service_dependencies_unhealthy"
_TOKEN = "gauge-detail-token"
_ALL = (FALKORDB, LLM_INTERFACE, CELLAR_ELI, PASSKEY_SIGNING_POSTGRES, STATE_POSTGRES, GRAPH_REPLAY)
_MIXES = (
    (),
    (FALKORDB,),
    (LLM_INTERFACE,),
    (FALKORDB, CELLAR_ELI),
    (STATE_POSTGRES, GRAPH_REPLAY, PASSKEY_SIGNING_POSTGRES),
    _ALL,
)


@pytest.fixture(autouse=True)
def _accept_the_gauge_token(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the verifier accept one token; the cryptographic path needs a real IdP signing key."""

    async def verify_token(self: PsTokenVerifier, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id="reader", scopes=[]) if token == _TOKEN else None

    # detroit-exception: the verifier's cryptographic path needs a real IdP signing key; the
    # `/ready` detail gate only depends on its accept/reject answer
    monkeypatch.setattr(PsTokenVerifier, "verify_token", verify_token)


def _gauge_values(client: TestClient) -> dict[str, float]:
    return {s.labels["gating"]: s.value for s in samples(client.get("/metrics").content, _GAUGE)}


def _ready_split(client: TestClient) -> dict[str, float]:
    response = client.get("/ready", headers={"Authorization": f"Bearer {_TOKEN}"})
    names: list[str] = response.json()["unhealthy_dependencies"]
    gating = sum(1 for name in names if name in _GATING_DEPENDENCIES)
    return {"true": float(gating), "false": float(len(names) - gating)}


@pytest.mark.parametrize("unhealthy", _MIXES)
def test_gauge_equals_the_authenticated_ready_split_for_each_mix(
    unhealthy: tuple[str, ...],
) -> None:
    client = TestClient(create_app(auth_enabled_config()))
    for name in unhealthy:
        mark_unhealthy(name, error=RuntimeError("down"))

    assert _gauge_values(client) == _ready_split(client)


def test_both_series_are_present_and_zero_when_everything_is_healthy() -> None:
    client = TestClient(create_app(auth_enabled_config()))

    assert _gauge_values(client) == {"true": 0.0, "false": 0.0}


def test_the_gauge_follows_the_ready_dependency_lists_not_a_hardcoded_set() -> None:
    client = TestClient(create_app(auth_enabled_config()))
    for name in _READY_DEPENDENCIES:
        mark_unhealthy(name, error=RuntimeError("down"))

    values = _gauge_values(client)

    assert values["true"] == float(len(_GATING_DEPENDENCIES))
    assert values["false"] == float(len(_READY_DEPENDENCIES) - len(_GATING_DEPENDENCIES))


def test_no_dependency_name_or_error_text_reaches_the_scrape() -> None:
    client = TestClient(create_app(auth_enabled_config()))
    for name in _ALL:
        mark_unhealthy(name, error=RuntimeError("secret-host.internal:5432 refused"))

    body = client.get("/metrics").text

    assert "secret-host" not in body
    for name in _ALL:
        assert name not in body


def test_a_scrape_leaves_the_dependency_registry_unchanged() -> None:
    client = TestClient(create_app(auth_enabled_config()))
    mark_unhealthy(FALKORDB, error=RuntimeError("down"))
    before = {name: dependency_health.is_healthy(name) for name in _ALL}

    for _ in range(3):
        client.get("/metrics")

    assert {name: dependency_health.is_healthy(name) for name in _ALL} == before


def test_a_failing_counts_source_omits_the_family_and_the_scrape_stays_200(
    configured_logging: Path,
) -> None:
    def broken() -> tuple[int, int]:
        raise RuntimeError("boom-detail /var/secret")

    handle = build_metrics(unhealthy_counts=broken)

    body, _content_type = handle.render()

    assert _GAUGE not in body.decode()
    assert b"boom-detail" not in body
    handle.shutdown()
    [entry] = metrics_entries(configured_logging, "collect_dependencies_unhealthy")
    assert entry["error_type"] == "RuntimeError"
    assert "boom-detail" not in json.dumps(entry)
