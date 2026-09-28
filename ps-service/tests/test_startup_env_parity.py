"""Proves the container smoke test's env vars are sufficient to boot `create_app` (issue #148).

Release to Production run #56 (v3.1.0) failed both arch builds: issue #140 added a new
unconditional config-completeness check (`require_authentik_credential_configured`, AC-BI-003),
its env vars were added to every fast fixture that constructs a `ServiceConfig` or sets env
vars for `create_app`, except `test_container_image.py`'s `docker run --env` list -- the one
consumer that boots the actual packaged image. Nothing caught that until the release tag build
itself tried to boot the image (issue #148).

This test closes that gap without Docker: `REQUIRED_STARTUP_ENV` (`ps_test_support`) is the
exact same dict `test_container_image.py` passes to every `docker run`, so proving `create_app`
succeeds here, under the identical env, is a hermetic proxy for the real container boot. It runs
in the default suite -- unlike `test_container_image.py`, which carries the `container_image`
marker and is excluded from every stage except the release tag build (see `pyproject.toml`'s
`addopts`) -- so a future unconditional check that isn't added to `REQUIRED_STARTUP_ENV` now
fails on every branch's CI, not only when a release is actually cut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.config import load_config
from ps_service.main import create_app
from ps_test_support.required_startup_env import REQUIRED_STARTUP_ENV

if TYPE_CHECKING:
    import pytest


def test_container_smoke_env_boots_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact env `test_container_image.py` passes to `docker run` must let `create_app` boot.

    Mirrors `_start_service`/`catalog_only_service`'s fixed env exactly: `PS_SERVICE_HOST`
    pinned to loopback, `PS_SERVICE_LOCAL_TEST_BYPASS=true` (no OIDC credentials, issue #67),
    and `REQUIRED_STARTUP_ENV` on top. `PS_FALKORDB_HOST` is deliberately absent -- it only
    affects runtime dependency health, never `create_app`'s synchronous startup checks.
    """
    monkeypatch.setenv("PS_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    for key, value in REQUIRED_STARTUP_ENV.items():
        monkeypatch.setenv(key, value)

    create_app(load_config())
