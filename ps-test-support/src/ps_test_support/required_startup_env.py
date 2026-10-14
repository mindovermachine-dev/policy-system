"""Env vars `ps_service.main.create_app` requires unconditionally to boot.

Purpose: `create_app` runs a handful of synchronous, fail-closed config-completeness checks
before it ever returns an app. Most of them (OIDC auth, `require_bootstrap_owner_configured`)
are *bypassable* via `PS_SERVICE_LOCAL_TEST_BYPASS=true`.
`require_authentik_credential_configured` deliberately is not: it fails closed regardless of
the bypass. Tests that boot the app therefore must always supply those variables.

`REQUIRED_STARTUP_ENV` is the single, shared source of that unconditional category: a dict
mapping each env var name to a placeholder value that satisfies its check (non-empty
strings; the base URL is a syntactically valid but unresolvable `.invalid` host). It is
imported both by `ps-service/tests/test_container_image.py` (which passes these as `docker run
--env` flags to the real, packaged image) and by
`ps-service/tests/test_startup_env_parity.py` (a fast, hermetic test that proves the exact same
env is enough for `create_app` to succeed, with no Docker involved). Hand-copied lists drift
out of sync with the checks, so a future unconditional check should extend this dict instead of
hand-copying a new env var into each consumer: the parity test then fails on every branch's CI
the moment a consumer falls out of sync, rather than only at the release build.

Side effects: none; importing the module only builds the dict. Consumers must copy it before
mutating it, since it is shared module state.
"""

from __future__ import annotations

REQUIRED_STARTUP_ENV: dict[str, str] = {
    "PS_AUTHENTIK_API_TOKEN": "ps-smoke-test-token",
    "PS_AUTHENTIK_BASE_URL": "https://authentik.invalid",
}
