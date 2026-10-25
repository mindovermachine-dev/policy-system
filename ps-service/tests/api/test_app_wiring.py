"""Tests that `create_app` mounts the REST router without disturbing the harness routes.

`POST /ingestions` is mounted. `GET /regulations` does not exist and must be absent
from both the OpenAPI schema and the live route table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI


def _make_app() -> FastAPI:
    return create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )


def test_create_app_registers_post_ingestions_and_not_regulations() -> None:
    """`create_app` mounts `POST /ingestions` and not `GET /regulations`.

    Introspects the generated OpenAPI schema rather than walking `app.routes`:
    FastAPI 0.141 includes a sub-router lazily (an opaque `_IncludedRouter`
    entry), so `app.routes` does not carry a flattened `APIRoute` per
    mounted path, but the OpenAPI `paths` map still does. The direct HTTP
    call is the real proof the route is unreachable, not just absent from
    the schema; the schema assertion covers the documented surface.
    """
    app = _make_app()
    client = TestClient(app)
    schema = app.openapi()

    assert "post" in schema["paths"]["/ingestions"]
    assert "/regulations" not in schema["paths"]
    assert client.get("/regulations").status_code == 404


def test_create_app_still_serves_the_probes_and_status() -> None:
    """Mounting the REST router leaves `/livez`, `/readyz` and `/status` intact.

    `/readyz` is queried via a bare (never-entered) `TestClient`, so `lifespan`
    startup never runs and `app.state.ready` stays its `False` default —
    a not-ready `/readyz` returns 503, not 200, while `/status` still answers 200.
    """
    client = TestClient(_make_app())

    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 503
    assert client.get("/status").status_code == 200


def test_create_app_serves_status_through_the_api_router() -> None:
    """`GET /status` is part of the REST API router's documented surface and answers 200."""
    app = _make_app()
    client = TestClient(app)
    schema = app.openapi()

    assert "get" in schema["paths"]["/status"]
    response = client.get("/status")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "version"}
