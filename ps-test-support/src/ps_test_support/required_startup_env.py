"""Env vars `ps_service.main.create_app` requires unconditionally to boot (issue #148).

`create_app` runs a handful of synchronous, fail-closed config-completeness checks before it
ever returns an app. Most of them (OIDC auth, `require_bootstrap_owner_configured`) are
*bypassable* via `PS_SERVICE_LOCAL_TEST_BYPASS=true`. `require_authentik_credential_configured`
(issue #140) deliberately is not: it fails closed regardless of the bypass (AC-BI-003).

`REQUIRED_STARTUP_ENV` is the single, shared source of that unconditional category. It is
imported both by `ps-service/tests/test_container_image.py` (which passes these as `docker run
--env` flags to the real, packaged image) and by
`ps-service/tests/test_startup_env_parity.py` (a fast, hermetic test that proves the exact same
env is enough for `create_app` to succeed, with no Docker involved). Before this module
existed, each consumer hand-copied its own list of "env vars `create_app` needs" -- issue #140
added its pair to every one of them except the container smoke test's `docker run` command, and
nothing caught that gap until an actual release build failed to boot (issue #148, Release to
Production run #56). A future unconditional check should extend this dict instead of
hand-copying a new env var into each consumer: the parity test then fails on every branch's CI
the moment a consumer falls out of sync, rather than only at the release tag build.
"""

from __future__ import annotations

REQUIRED_STARTUP_ENV: dict[str, str] = {
    "PS_AUTHENTIK_API_TOKEN": "ps-smoke-test-token",
    "PS_AUTHENTIK_BASE_URL": "https://authentik.invalid",
}
