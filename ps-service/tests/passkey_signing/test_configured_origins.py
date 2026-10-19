"""Configured allowed origins: relying-party values and Host rejection on the POST routes.

`rp_id_and_origin` is exercised on hand-built requests; the five POST routes are driven through
`TestClient` with fake stores. A store that raises on any read proves a rejected Host never
reaches the approval store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store
from ps_service.passkey_signing.webauthn_rp import rp_id_and_origin

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow


def _config(*origins: str) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
        passkey_signing_allowed_origins=origins,
    )


def _request(host: str | None, *, config: ServiceConfig, scheme: str = "http") -> Request:
    headers = [(b"host", host.encode())] if host is not None else []
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "method": "POST",
            "path": "/approvals/x/summary",
            "query_string": b"",
            "headers": headers,
            "server": ("127.0.0.1", 8000),
            "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        }
    )


# --- relying-party values -------------------------------------------------------------------


def test_unconfigured_localhost_8000_yields_legacy_rp_id_and_origin() -> None:
    request = _request("localhost:8000", config=_config())

    assert rp_id_and_origin(request) == ("localhost", "http://localhost:8000")


def test_unconfigured_bracketed_and_arbitrary_hosts_keep_the_legacy_derivation() -> None:
    assert rp_id_and_origin(_request("203.0.113.9:30443", config=_config(), scheme="https")) == (
        "203.0.113.9",
        "https://203.0.113.9:30443",
    )


def test_first_configured_origin_gives_rp_id_and_origin_not_the_request() -> None:
    config = _config("https://ps.test", "http://localhost:8000")

    assert rp_id_and_origin(_request("ps.test", config=config, scheme="http")) == (
        "ps.test",
        "https://ps.test",
    )


def test_configured_origin_with_a_port_keeps_it_in_the_origin_but_not_in_the_rp_id() -> None:
    config = _config("https://ps.test:30443")

    assert rp_id_and_origin(_request("ps.test:30443", config=config)) == (
        "ps.test",
        "https://ps.test:30443",
    )


def test_second_configured_origin_host_is_accepted_but_rp_id_stays_first() -> None:
    config = _config("https://ps.test", "https://alt.ps.test")

    rp_id, origin = rp_id_and_origin(_request("alt.ps.test", config=config))

    assert rp_id == "ps.test"
    assert origin == "https://alt.ps.test"


def test_configured_ipv6_origin_rp_id_has_no_brackets() -> None:
    config = _config("http://[::1]:8000")

    assert rp_id_and_origin(_request("[::1]:8000", config=config)) == ("::1", "http://[::1]:8000")


# --- Host rejection on the five POST routes ------------------------------------------------


class _ExplodingStore(FakePendingApprovalStore):
    """A pending-approval store that fails the test if a rejected request reaches it."""

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        raise AssertionError(f"store read for {pending_approval_id}")


def _exploding_store() -> FakePendingApprovalStore:
    return _ExplodingStore()


def _pending_store() -> FakePendingApprovalStore:
    return FakePendingApprovalStore()


def _credential_store() -> FakeSigningCredentialStore:
    return FakeSigningCredentialStore()


_ROUTES: list[tuple[str, dict[str, object]]] = [
    ("summary", {"code": "c"}),
    ("enroll/options", {"code": "c"}),
    ("enroll/verify", {"code": "c", "credential": {}}),
    ("sign/options", {"code": "c"}),
    ("sign/verify", {"code": "c", "credential": {}}),
]
_INVALID_CODE = "pending_approval_invalid_or_expired"


def _client(*origins: str, base_url: str) -> TestClient:
    app = create_app(_config(*origins))
    app.dependency_overrides[provide_pending_approval_store] = _exploding_store
    app.dependency_overrides[provide_signing_credential_store] = _credential_store
    return TestClient(app, base_url=base_url)


@pytest.mark.parametrize(("route", "body"), _ROUTES)
def test_a_host_outside_the_configured_origins_is_rejected_with_421(
    route: str, body: dict[str, object]
) -> None:
    configure()
    client = _client("https://ps.test", base_url="http://evil.test")

    response = client.post(f"/approvals/some-id/{route}", json=body)

    assert response.status_code == 421
    payload = response.json()
    assert payload["error"]["code"] == "signing_host_not_allowed"
    assert "evil.test" not in response.text
    assert "ps.test" not in response.text


@pytest.mark.parametrize(("route", "body"), _ROUTES)
def test_an_allowed_host_passes_the_host_check_and_reaches_the_code_check(
    route: str, body: dict[str, object]
) -> None:
    configure()
    app = create_app(_config("http://ps.test"))
    app.dependency_overrides[provide_pending_approval_store] = _pending_store
    app.dependency_overrides[provide_signing_credential_store] = _credential_store
    client = TestClient(app, base_url="http://ps.test")

    response = client.post(f"/approvals/some-id/{route}", json=body)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_CODE


def test_host_comparison_ignores_case_and_default_ports() -> None:
    configure()
    app = create_app(_config("https://ps.test", "http://other.test:80"))
    app.dependency_overrides[provide_pending_approval_store] = _pending_store
    app.dependency_overrides[provide_signing_credential_store] = _credential_store

    for host in ("PS.TEST", "ps.test:443", "other.test", "OTHER.test:80"):
        client = TestClient(app)
        response = client.post(
            "/approvals/some-id/summary", json={"code": "c"}, headers={"host": host}
        )
        assert response.status_code == 404, host


def test_a_non_default_port_on_the_host_is_rejected() -> None:
    configure()
    client = _client("https://ps.test", base_url="https://ps.test:8443")

    response = client.post("/approvals/some-id/summary", json={"code": "c"})

    assert response.status_code == 421


def test_a_bracketed_ipv6_host_matches_its_configured_origin() -> None:
    configure()
    app = create_app(_config("http://[::1]:8000"))
    app.dependency_overrides[provide_pending_approval_store] = _pending_store
    app.dependency_overrides[provide_signing_credential_store] = _credential_store
    client = TestClient(app)

    response = client.post(
        "/approvals/some-id/summary", json={"code": "c"}, headers={"host": "[::1]:8000"}
    )

    assert response.status_code == 404


def test_a_forged_forwarded_host_header_does_not_make_a_host_allowed() -> None:
    configure()
    client = _client("https://ps.test", base_url="http://evil.test")

    response = client.post(
        "/approvals/some-id/summary",
        json={"code": "c"},
        headers={"x-forwarded-host": "ps.test", "forwarded": "host=ps.test"},
    )

    assert response.status_code == 421


def test_no_configured_origins_accepts_any_host_on_the_post_routes() -> None:
    configure()
    app = create_app(_config())
    app.dependency_overrides[provide_pending_approval_store] = _pending_store
    app.dependency_overrides[provide_signing_credential_store] = _credential_store
    client = TestClient(app, base_url="http://anything.test")

    response = client.post("/approvals/some-id/summary", json={"code": "c"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_CODE


def test_the_html_shell_get_route_is_not_host_restricted() -> None:
    client = _client("https://ps.test", base_url="http://evil.test")

    assert client.get("/approvals/some-id").status_code == 200
