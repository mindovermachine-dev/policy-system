"""Route allow-list default-deny audit (issue #58, Slice 9, AC-BI-011).

AC-BI-011: "WHEN any route not in the explicit open-route allow-list
(``/health``, ``/ready``, ``/.well-known/*``) is registered THEN it requires
a token -- a test enumerates the app's routes and asserts this."

This is the one test that proves default-deny holds for **every** registered
route, present and future, rather than the single representative route
(``GET /catalog``) `test_rest_auth_middleware.py` (Slice 3) exercises.
`create_app`'s own composition root is what's audited -- if a future route is
added to `build_api_router()`/`create_app()` without going through
`RestAuthMiddleware`, this test fails loudly by construction (it enumerates
whatever `app.openapi()["paths"]` reports, not a hardcoded list).

Enumeration technique mirrors `test_app_wiring.py`'s own comment: on this
FastAPI version, `app.routes` no longer carries a flattened `APIRoute` entry
per mounted path, but `app.openapi()["paths"]` still does.

``/mcp`` is a `Starlette` `Mount`, not an `APIRoute` -- it never appears in
`app.openapi()["paths"]`. `RestAuthMiddleware` deliberately exempts
``/mcp*`` from its own check (`ps_service/auth/middleware.py`'s
`_MCP_MOUNT_PREFIX`) because the MCP SDK's own `token_verifier=` gate
(Slice 5, `RequireAuthMiddleware`/`BearerAuthBackend`) already enforces auth
*inside* the mounted sub-app with the same shared `PsTokenVerifier`. The
explicit probe below proves that exemption is a deliberate hand-off to a
second, still-active gate -- not a silent bypass -- by hitting the real
`create_app()` composition root's `/mcp/` mount directly with no
`Authorization` header and confirming the *inner* gate still rejects it,
with the MCP-side rejection shape (a `WWW-Authenticate: Bearer ...` header;
the MCP SDK's own body shape, not REST's `_error_body` JSON -- see
`tests/mcp_interface/test_mcp_auth.py`, Slice 5, for the same observed
shape).

Drives the real composition root (`create_app`) against
`tests.auth.mock_oidc_provider`'s real local HTTP server -- no monkeypatched
transport anywhere in this file (AC-BI-017), matching every other Slice's
convention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ps_service.config import ServiceConfig
from ps_service.main import create_app
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH

# `mock_oidc_provider_fixture` registers pytest's "mock_oidc_provider" fixture
# (see `tests.auth.mock_oidc_provider`'s module docstring for why it is
# imported under this name, not `mock_oidc_provider` itself, which every test
# below declares as a same-named parameter instead) -- never called directly.
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from pathlib import Path

    from ps_test_support.mock_oidc_provider import MockOidcProvider

_AUDIENCE = "ps-service"


@pytest.fixture(autouse=True)
def _configure_logging_for_auth_tests(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,
) -> None:
    """Slice 10: `PsTokenVerifier.verify_token` now always logs (AC-BI-014/015)
    on every request this audit fires -- install a real process-wide Logging
    facade (`tests/api/conftest.py`'s own `configured_logging` fixture) so
    `emit_log_entry`'s no-default-configured guard never trips here.
    """


_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_JSON_RPC_ACCEPT = "application/json, text/event-stream"

# AC-BI-011's own verbatim allow-list -- deliberately re-declared here rather than
# imported from `ps_service.auth.middleware`, so this test fails (rather than
# silently passing) if a future edit to that module's private constants ever drifts
# from the issue's own wording.
_EXEMPT_PATHS = frozenset({"/health", "/ready"})
_EXEMPT_PREFIX = "/.well-known/"

# Path parameters this audit substitutes into templated OpenAPI paths (e.g.
# "/ingestions/{run_id}") to build a concrete, requestable URL. The values
# themselves are arbitrary -- the request is rejected by `RestAuthMiddleware`
# before any path-parameter validation or handler code ever runs.
_PATH_PARAM_VALUES = {"run_id": "test-run-id", "review_id": "test-review-id"}

# One syntactically-valid body per POST route, keyed by its templated OpenAPI
# path -- a minimal-but-schema-valid payload, so a 422 (bad input) can never
# be mistaken for the 401 under test if the auth gate were ever accidentally
# moved to run after body validation.
_REQUEST_BODIES: dict[str, dict[str, object]] = {
    "/ingestions": {"source": "catalog", "celex": "32019R0881"},
    "/restorations": {
        "instrument_id": "test-instrument",
        "manifest": {
            "instrument_id": "test-instrument",
            "celex": None,
            "title": "Test Title",
            "short_name": "test",
            "version": "1",
            "source_type": "internal",
            "jurisdiction": None,
            "schema_version": "1",
            "exported_at": "2024-01-01T00:00:00Z",
            "baseline_sha256": "0" * 64,
            "native_sha256": "0" * 64,
        },
        "baseline_blob_base64": "YQ==",
        "native_blob_base64": "YQ==",
    },
    "/exports": {"instrument_id": "test-instrument"},
    "/near-misses/{review_id}/resolve": {"decision": "keep-separate"},
    # "/change-checks" takes no request body at all -- absent from this map.
}


def _config(provider: MockOidcProvider, **overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": False,
        "auth_issuer": provider.issuer,
        "auth_audience": _AUDIENCE,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def _is_exempt(path: str) -> bool:
    """Mirror `RestAuthMiddleware`'s own exemption check, verbatim against AC-BI-011's wording."""
    return path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIX)


def _concretize(templated_path: str) -> str:
    """Substitute every ``{param}`` placeholder with an arbitrary concrete value."""
    concrete = templated_path
    for name, value in _PATH_PARAM_VALUES.items():
        concrete = concrete.replace(f"{{{name}}}", value)
    return concrete


def _request_body(templated_path: str) -> dict[str, object] | None:
    return _REQUEST_BODIES.get(templated_path)


def test_every_non_exempt_route_requires_a_token(mock_oidc_provider: MockOidcProvider) -> None:
    """AC-BI-011: every registered route outside the allow-list rejects an unauthenticated
    request with 401 -- never 404 (route missing), 422 (masked by bad input), or 200.
    """
    app = create_app(_config(mock_oidc_provider))
    client = TestClient(app)
    schema = app.openapi()

    checked: list[str] = []
    for templated_path, path_item in schema["paths"].items():
        if _is_exempt(templated_path):
            continue
        concrete_path = _concretize(templated_path)
        for method in path_item:
            if method not in _HTTP_METHODS:
                continue
            response = client.request(
                method.upper(), concrete_path, json=_request_body(templated_path)
            )
            assert response.status_code == 401, (
                f"{method.upper()} {concrete_path} returned {response.status_code}, "
                "expected 401 (default-deny, AC-BI-011)"
            )
            checked.append(f"{method.upper()} {concrete_path}")

    # A schema that suddenly reported zero non-exempt routes would make the loop
    # above vacuously pass -- guard against that silently swallowing a real gap.
    assert checked, "expected at least one non-exempt route to audit"


def test_open_allowlist_routes_remain_reachable_without_a_token(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """The flip side of the audit above: the allow-listed routes are genuinely open.

    Proves `RestAuthMiddleware`'s exemption is not accidentally over-broad in
    the *other* direction either -- these three stay reachable with no
    `Authorization` header at all, exactly as AC-BI-011's allow-list promises.
    """
    app = create_app(_config(mock_oidc_provider))
    client = TestClient(app)

    assert client.get("/health").status_code != 401
    assert client.get("/ready").status_code != 401
    assert client.get("/.well-known/oauth-protected-resource").status_code != 401


def test_mcp_mount_rejects_unauthenticated_request_via_its_own_sdk_gate(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`/mcp*` is exempted from `RestAuthMiddleware`'s own check by design (it is not an
    `APIRoute`, so it can never appear in the enumeration above) -- this proves the
    exemption is a hand-off to a second, still-enforcing gate, not a silent bypass.

    Hits the real `create_app()` composition root's mounted transport directly (not
    the bespoke standalone-`Starlette`-wrapper helper `test_mcp_auth.py` uses), so
    this is the same object AC-BI-011's "any route ... is registered" language
    describes for the whole app, `/mcp` included.
    """
    app = create_app(_config(mock_oidc_provider))

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": _JSON_RPC_ACCEPT},
        )

    # The MCP SDK's own `RequireAuthMiddleware` rejects this before the `initialize`
    # handshake ever completes -- observed shape per `test_mcp_auth.py` (Slice 5):
    # 401 with a `WWW-Authenticate: Bearer ...` header, not REST's `_error_body` JSON
    # shape (the two gates are deliberately allowed to differ, PLAN.md §1.3).
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")
