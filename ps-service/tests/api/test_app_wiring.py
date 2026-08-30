"""Tests that `create_app` mounts the REST router without disturbing the harness routes.

Both REST routes are now mounted: `GET /regulations` (Increment 1) and
`POST /ingestions` (Increment 6).
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
        )
    )


def test_create_app_registers_get_regulations_and_post_ingestions() -> None:
    """AC-BI-001: `create_app` mounts `GET /regulations` and `POST /ingestions`.

    Introspects the generated OpenAPI schema rather than walking `app.routes`:
    FastAPI 0.141 includes a sub-router lazily (an opaque `_IncludedRouter`
    entry), so `app.routes` no longer carries a flattened `APIRoute` per
    mounted path, but the OpenAPI `paths` map still does.
    """
    schema = _make_app().openapi()

    assert "get" in schema["paths"]["/regulations"]
    assert "post" in schema["paths"]["/ingestions"]


def test_create_app_still_serves_health_and_ready() -> None:
    """Regression: mounting the REST router leaves `/health` and `/ready` intact."""
    client = TestClient(_make_app())

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
