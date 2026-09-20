"""Tests that `create_app` mounts the REST router without disturbing the harness routes.

`POST /ingestions` is mounted (Increment 6). `GET /regulations` (Increment 1)
was removed (issue #78) and must be absent from both the OpenAPI schema and
the live route table.
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
        )
    )


def test_create_app_registers_post_ingestions_and_not_regulations() -> None:
    """AC-BI-003/004/008: `create_app` mounts `POST /ingestions` and not `GET /regulations`.

    Introspects the generated OpenAPI schema rather than walking `app.routes`:
    FastAPI 0.141 includes a sub-router lazily (an opaque `_IncludedRouter`
    entry), so `app.routes` no longer carries a flattened `APIRoute` per
    mounted path, but the OpenAPI `paths` map still does. The direct HTTP
    call is the real proof the route is unreachable, not just absent from
    the schema (AC-BI-003); the schema assertion covers AC-BI-004.
    """
    app = _make_app()
    client = TestClient(app)
    schema = app.openapi()

    assert "post" in schema["paths"]["/ingestions"]
    assert "/regulations" not in schema["paths"]
    assert client.get("/regulations").status_code == 404


def test_create_app_still_serves_health_and_ready() -> None:
    """Regression: mounting the REST router leaves `/health` and `/ready` intact.

    `/ready` is queried via a bare (never-entered) `TestClient`, so `lifespan`
    startup never runs and `app.state.ready` stays its `False` default —
    since issue #75, a not-ready `/ready` returns 503, not 200 (see
    `ps_service.main.ready`'s docstring).
    """
    client = TestClient(_make_app())

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
