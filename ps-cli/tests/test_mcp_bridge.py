"""Tests for ps_cli.mcp_bridge (issue #118): structured lifecycle/health logging.

`_BridgeContext`, `_forward_message`, `_log`, `_open_log_file` are private helpers,
unit-tested directly per this repo's existing precedent (`test_config.py`'s
`_deep_merge`) -- imported with `# pyright: ignore[reportPrivateUsage]`. `main()` is
the module's one real public entry point (the `ps-cli-mcp-bridge` console script) and
is exercised end-to-end via a real `httpx.MockTransport` and a stubbed `sys.stdin`,
mirroring `device_flow.py`'s own `transport` constructor-injection seam.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import re
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

import httpx
import msal_extensions.persistence
import pytest

from ps_cli.bridge_credentials import BridgeCredentialView
from ps_cli.credentials import PersistenceCredentialStore, TokenBundle
from ps_cli.device_flow import AccessTokenCache
from ps_cli.mcp_bridge import (
    _LOG_FILE_NAME,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _BridgeContext,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _forward_message,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _log,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _open_log_file,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    main,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import TextIO

    from conftest import FakeKeychainError, InMemoryPersistenceBackend

_MCP_URL = "https://ps.example.test/mcp/"
_SERVICE_URL = "https://ps.example.test"

# Issue #121 Slice 2's own fake issuer/discovery constants -- mirrors
# `test_http_client.py`'s `_build_auth_and_business_transport` group (CHANGES.md
# MINOR-2's "fake-transport, refresh-call-counting" design), applied at the
# mcp_bridge entrypoint via `_BridgeContext.transport` (the seam `_resolve_access_token`
# threads into `ensure_valid_access_token`, added in this slice -- see mcp_bridge.py).
_FAKE_ISSUER = "https://issuer.example"
_FAKE_CLIENT_ID = "ps-cli-test-client"
_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_URL = f"{_FAKE_ISSUER}/.well-known/openid-configuration"
_TOKEN_URL = f"{_FAKE_ISSUER}/token"


def _in_memory_credential_store(
    build_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> PersistenceCredentialStore:
    """A fresh, portable `PersistenceCredentialStore` backed by an in-memory fake (D-121-7).

    Replaces `FileCredentialStore(tmp_path)` (issue #121: that class is gone entirely,
    AC-BI-008) -- just enough to keep this suite green; Slice 2 adds the
    bridge-specific reuse-across-messages/expiry-recovery proofs on top. Takes the
    shared `build_in_memory_persistence` fixture (`conftest.py`) as a parameter rather
    than constructing one itself -- pytest's `--import-mode=importlib` (this repo's
    convention, `pyproject.toml`) means `conftest.py`'s classes can only be
    instantiated via fixture injection, never a direct `from conftest import ...` at
    module level in a test file.
    """
    return PersistenceCredentialStore(build_persistence=build_persistence)


def _view(
    store: PersistenceCredentialStore,
    *,
    warnings: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> BridgeCredentialView:
    """The one place the bridge's credential view is built for these tests (issue #181).

    `warnings`, when given, collects every message the view logs. `timeout_seconds`
    overrides the keychain-call bound (production default 5 s) for stall tests.
    """
    logger = warnings.append if warnings is not None else None
    if timeout_seconds is None:
        return BridgeCredentialView(store, logger=logger)
    return BridgeCredentialView(store, logger=logger, timeout_seconds=timeout_seconds)


def _ctx(
    tmp_path: Path,
    build_persistence: Callable[[str], InMemoryPersistenceBackend],
    *,
    context_name: str | None = None,
    log_file: TextIO | None = None,
    access_token_cache: AccessTokenCache | None = None,
) -> _BridgeContext:
    """Build a `_BridgeContext` with literal values -- no real config/service needed."""
    del tmp_path  # unused now that credential storage is persistence-backed, not file-based
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=_view(_in_memory_credential_store(build_persistence)),
        access_token_cache=(
            access_token_cache if access_token_cache is not None else AccessTokenCache()
        ),
        log_file=log_file,
    )


def _ctx_with_stale_bundle(
    build_persistence: Callable[[str], InMemoryPersistenceBackend],
    *,
    context_name: str,
    log_file: TextIO | None = None,
) -> _BridgeContext:
    """A `_BridgeContext` whose stored bundle has no `refresh_token` -- the very next
    `ensure_valid_access_token` call fails closed (AC-BI-002), with an empty (never
    populated) `AccessTokenCache`.
    """
    store = _in_memory_credential_store(build_persistence)
    store.set_tokens(context_name, TokenBundle(refresh_token=None, issuer="https://idp.example"))
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=_view(store),
        access_token_cache=AccessTokenCache(),
        log_file=log_file,
    )


def _ctx_with_cached_access_token(
    build_persistence: Callable[[str], InMemoryPersistenceBackend],
    *,
    context_name: str,
    access_token: str,
    log_file: TextIO | None = None,
) -> _BridgeContext:
    """A `_BridgeContext` whose `access_token_cache` is pre-populated with `access_token`.

    `ensure_valid_access_token`'s cache-hit branch then returns `access_token`
    immediately, with no refresh network call -- `mcp_bridge.py` has no `transport`
    seam into the refresh path (see its own module docstring), so a real refresh
    cannot be simulated here without adding one; pre-populating the cache is the
    direct, minimal way to exercise `_forward_message`'s own bearer-attachment
    behavior in isolation (issue #121, D-121-2). A stored bundle with a non-`None`
    `refresh_token` is also needed so `_resolve_access_token`'s own "nothing stored
    for this context" check doesn't short-circuit before ever consulting the cache.
    """
    store = _in_memory_credential_store(build_persistence)
    store.set_tokens(
        context_name, TokenBundle(refresh_token="rt-unused", issuer="https://idp.example")
    )
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=_view(store),
        access_token_cache=AccessTokenCache(token=access_token, expires_at=99999999999),
        log_file=log_file,
    )


def _build_auth_and_mcp_transport(
    mcp_handler: Callable[[httpx.Request], httpx.Response],
    *,
    presented_refresh_tokens: list[str] | None = None,
    expires_in: int = 3600,
    access_token_prefix: str = "refreshed-token",
    rotated_refresh_token: str = "rt-rotated",
) -> tuple[httpx.MockTransport, list[int]]:
    """One fake transport answering resource-metadata/discovery/refresh + the forwarded
    MCP business call (issue #121 Slice 2) -- the mcp_bridge-entrypoint analog of
    `test_http_client.py`'s own `_build_auth_and_business_transport`, per CHANGES.md
    MINOR-2's "fake-transport, refresh-call-counting" design (a genuine wire-level
    fake, not a `resolve_auth_parameters`/`_refresh_tokens` monkeypatch).

    Returns `(transport, call_count)` where `call_count[0]` is mutated on every
    `POST <issuer>/token` refresh call, so a test can assert exactly how many
    refreshes happened across any number of `_forward_message` calls sharing it.
    When `presented_refresh_tokens` is given, every refresh token the token endpoint
    receives is appended to it (issue #181: proves which stored token a refresh used).
    `expires_in` is the access-token lifetime the token endpoint reports (a value below
    the 30 s expiry leeway makes every refreshed token stale at once). The issued access
    token is `<access_token_prefix>-<n>` and the rotated refresh token is
    `rotated_refresh_token` (issue #181: lets AC-BI-010 tests plant recognisable sentinels).
    """
    call_count = [0]

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == _RESOURCE_METADATA_PATH:
            return httpx.Response(
                200,
                json={
                    "resource": _SERVICE_URL,
                    "authorization_servers": [_FAKE_ISSUER],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": _FAKE_CLIENT_ID,
                },
            )
        if str(request.url) == _OPENID_CONFIGURATION_URL:
            return httpx.Response(
                200,
                json={
                    "issuer": _FAKE_ISSUER,
                    "device_authorization_endpoint": f"{_FAKE_ISSUER}/device_authorization",
                    "token_endpoint": _TOKEN_URL,
                },
            )
        if str(request.url) == _TOKEN_URL:
            call_count[0] += 1
            if presented_refresh_tokens is not None:
                form = parse_qs(request.content.decode())
                presented_refresh_tokens.append(form["refresh_token"][0])
            return httpx.Response(
                200,
                json={
                    "access_token": f"{access_token_prefix}-{call_count[0]}",
                    "refresh_token": rotated_refresh_token,
                    "expires_in": expires_in,
                    "token_type": "Bearer",
                },
            )
        return mcp_handler(request)

    return httpx.MockTransport(_handle), call_count


def _ctx_with_refresh_token_and_transport(
    build_persistence: Callable[[str], InMemoryPersistenceBackend],
    *,
    context_name: str,
    transport: httpx.BaseTransport,
    warnings: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> _BridgeContext:
    """A `_BridgeContext` with a stored `refresh_token` and no cached access token yet,
    wired to `transport` so `ensure_valid_access_token`'s real refresh path (not a
    pre-populated cache) is what `_forward_message` actually exercises (issue #121
    Slice 2) -- distinct from `_ctx_with_cached_access_token` above, which exists
    specifically to bypass the refresh path for tests that don't care about it.
    """
    store = _in_memory_credential_store(build_persistence)
    store.set_tokens(context_name, TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=_view(store, warnings=warnings, timeout_seconds=timeout_seconds),
        access_token_cache=AccessTokenCache(),
        log_file=None,
        transport=transport,
    )


# --- Group 5: Issue #121 Slice 2 -- one refresh reused across many forwarded messages ---


class TestOneRefreshReusedAcrossManyForwardedMessages:
    """AC-BI-003/004 at the mcp_bridge entrypoint's own scale: many forwarded JSON-RPC
    messages per long-lived proxy-loop invocation, distinct from Slice 1's proof
    (many `PsServiceClient` business calls per instance, a different object with a
    different call pattern).
    """

    def test_two_forwarded_messages_share_exactly_one_refresh_and_identical_bearer_header(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """PLAN.md Slice 2's original ask: two forwarded messages against a fake PS
        Service MCP endpoint requiring a bearer token trigger exactly one refresh,
        and both messages' `Authorization` headers are identical.
        """
        captured_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, refresh_calls = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            session_id=None,
            ctx=ctx,
        )
        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 2},
            session_id=None,
            ctx=ctx,
        )

        assert refresh_calls == [1]
        assert captured_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-1"]

    def test_stale_cached_token_between_two_forwarded_messages_triggers_a_second_refresh(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """CHANGES.md CRITICAL-1's own regression proof: a long-lived mcp_bridge
        process whose cached token goes stale mid-session (wall-clock advances past
        `expires_at` between two forwarded messages) self-heals on the very next
        forwarded message -- the refresh-endpoint call count becomes 2, and the
        second message's `Authorization` header carries the NEW token, not the
        first (stale) one reused forever.

        Simulates the wall-clock advance by mutating the shared `AccessTokenCache`'s
        `expires_at` directly to an already-past epoch between the two
        `_forward_message` calls -- `_is_cache_stale` reads `cache.expires_at`
        fresh on every call, so this is equivalent to, and simpler than,
        monkeypatching `time.time()` in `device_flow.py`.
        """
        captured_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, refresh_calls = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            session_id=None,
            ctx=ctx,
        )
        assert refresh_calls == [1]

        ctx.access_token_cache.expires_at = 1  # long-past Unix epoch -> stale

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 2},
            session_id=None,
            ctx=ctx,
        )

        assert refresh_calls == [2]
        assert captured_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-2"]


_TOOLS_CALL = "tools/call"


def _tools_call(message_id: int) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": _TOOLS_CALL, "id": message_id}


def _ok_mcp_handler(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})


class TestNoKeychainReadWhileCredentialUnchanged:
    """Issue #181 AC-BI-004: after the first message per bridge process (which reads
    once, unconditionally), a valid cached token plus an unchanged credential
    modification time means forwarding does no keychain read.
    """

    def test_valid_cached_token_and_unchanged_mtime_forwards_without_a_keychain_read(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        transport, refresh_calls = _build_auth_and_mcp_transport(_ok_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        backend = build_in_memory_persistence("prod")
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        loads_after_first_message = backend.loads

        replies = [
            _forward_message(client, _tools_call(n), session_id=None, ctx=ctx)[0] for n in (2, 3, 4)
        ]

        assert backend.loads == loads_after_first_message
        assert all(r is not None and "result" in r for r in replies)
        assert refresh_calls == [1]

    def test_own_refresh_write_does_not_trigger_a_reread_on_the_next_message(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        transport, refresh_calls = _build_auth_and_mcp_transport(_ok_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        backend = build_in_memory_persistence("prod")
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        assert backend.loads == 1  # the one unconditional first read; the refresh wrote
        ctx.access_token_cache.expires_at = 1  # force a cache miss on the next message

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        assert "result" in reply
        assert backend.loads == 1  # our own write's mtime bump did not cause a re-read
        assert refresh_calls == [2]  # message 2 refreshed from the in-memory rotated token

    def test_unchanged_mtime_messages_build_the_persistence_at_most_once(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """CHANGES F-1: the stat path reuses one cached backend, so messages 2-4 with a
        valid cache and unchanged mtime never call the persistence factory again.
        """
        builds: list[str] = []

        def _counting_factory(context: str) -> InMemoryPersistenceBackend:
            builds.append(context)
            return build_in_memory_persistence(context)

        transport, _ = _build_auth_and_mcp_transport(_ok_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            _counting_factory, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        builds_before = len(builds)
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        builds_after_first = len(builds)

        for n in (2, 3, 4):
            _forward_message(client, _tools_call(n), session_id=None, ctx=ctx)

        assert len(builds) == builds_after_first
        # message 1 only: the one stat build, the one read, and its own refresh write
        assert builds_after_first - builds_before <= 3


class TestLogoutAndNeverStoredCredential:
    """Issue #181 AC-BI-001/002: a credential that disappears under a running bridge
    fails closed; a named context that never had one still forwards unauthenticated.
    """

    def test_logout_between_messages_fails_closed_with_login_hint_and_forwards_nothing(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        business_auth_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            business_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, _ = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        assert first is not None
        assert "result" in first
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).delete_tokens(
            "prod"
        )

        second, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
        loads_after_logout_message = build_in_memory_persistence("prod").loads
        third, _ = _forward_message(client, _tools_call(3), session_id=None, ctx=ctx)

        for reply in (second, third):
            assert reply is not None
            error = cast("dict[str, object]", reply["error"])
            assert error["code"] == -32001
            assert "no stored credentials for context" in cast("str", error["message"])
            assert "ps-cli auth login" in cast("str", error["message"])
        assert business_auth_headers == ["Bearer refreshed-token-1"]  # nothing forwarded after
        assert build_in_memory_persistence("prod").loads == loads_after_logout_message

    def test_named_context_with_nothing_ever_stored_still_forwards_unauthenticated(
        self,
        tmp_path: Path,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    ) -> None:
        """AC-BI-002 guard (local-test bypass server: nothing to log into)."""
        business_auth_headers: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            business_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        ctx = _ctx(tmp_path, build_in_memory_persistence, context_name="local-test")
        client = httpx.Client(transport=httpx.MockTransport(_handler))

        backend = build_in_memory_persistence("local-test")

        first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        loads_after_first_message = backend.loads
        later = [
            _forward_message(client, _tools_call(n), session_id=None, ctx=ctx)[0] for n in (2, 3)
        ]

        assert all(r is not None and "result" in r for r in [first, *later])
        assert business_auth_headers == [None, None, None]
        assert backend.loads == loads_after_first_message  # the one (retried) first read only

    def test_transient_not_found_after_a_seen_credential_does_not_latch_gone(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """CHANGES F-7: a not-found read after a seen credential (a rewrite caught
        mid-flight) keeps the last-known credential and is retried next message --
        only the empty logout sentinel means gone.
        """
        business_auth_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            business_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, _ = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        backend = build_in_memory_persistence("prod")
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        # foreign rewrite of the same bundle (moves the mtime), read caught mid-rewrite
        backend.save(backend.load())
        backend.fail_load_with = msal_extensions.persistence.PersistenceNotFound(
            message="caught mid-rewrite", location=backend.get_location()
        )
        ctx.access_token_cache.expires_at = 1

        during, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
        backend.fail_load_with = None
        after, _ = _forward_message(client, _tools_call(3), session_id=None, ctx=ctx)

        assert during is not None
        assert "result" in during
        assert after is not None
        assert "result" in after
        assert business_auth_headers == [
            "Bearer refreshed-token-1",
            "Bearer refreshed-token-2",
            "Bearer refreshed-token-2",
        ]


class TestReloginUnderARunningBridge:
    """Issue #181 AC-BI-003: a foreign rewrite of the credential with a different refresh
    token drops the cached access token; a rewrite of the same refresh token does not.
    """

    def test_relogin_rewrites_credential_and_next_message_uses_a_token_from_the_new_refresh_token(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        business_auth_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            business_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        presented: list[str] = []
        transport, refresh_calls = _build_auth_and_mcp_transport(
            _mcp_handler, presented_refresh_tokens=presented
        )
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).set_tokens(
            "prod", TokenBundle(refresh_token="rt-new", issuer=_FAKE_ISSUER)
        )

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        assert "result" in reply
        assert presented == ["seed-rt", "rt-new"]
        assert business_auth_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-2"]
        assert refresh_calls == [2]

    def test_rewrite_with_the_same_refresh_token_keeps_the_cached_access_token(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        business_auth_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            business_auth_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, refresh_calls = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)
        backend = build_in_memory_persistence("prod")
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        loads_before = backend.loads
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).set_tokens(
            "prod", TokenBundle(refresh_token="rt-rotated", issuer=_FAKE_ISSUER)
        )  # what message 1's refresh persisted: same refresh token, new mtime

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        assert "result" in reply
        assert backend.loads > loads_before  # the mtime change did cause a re-read
        assert refresh_calls == [1]
        assert business_auth_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-1"]


class TestKeychainReadFailures:
    """Issue #181 AC-BI-005/008: reply wording and the -67701 fallback for read errors."""

    def test_forward_message_minus_67701_on_first_read_replies_with_login_hint(
        self,
        tmp_path: Path,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        fake_keychain_error: type[FakeKeychainError],
    ) -> None:
        backend = build_in_memory_persistence("prod")
        backend.save('{"refresh_token": "seed-rt", "issuer": "https://idp.example"}')
        backend.fail_load_with = fake_keychain_error(-67701)
        ctx = _ctx(tmp_path, build_in_memory_persistence, context_name="prod")
        client = httpx.Client(transport=httpx.MockTransport(_ok_mcp_handler))

        reply, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)

        assert reply is not None
        error = cast("dict[str, object]", reply["error"])
        assert error["code"] == -32001
        assert "ps-cli auth login" in cast("str", error["message"])
        assert "unlocked" not in cast("str", error["message"])

    def _primed_ctx_with_foreign_bump(
        self,
        build_persistence: Callable[[str], InMemoryPersistenceBackend],
        warnings: list[str],
    ) -> tuple[_BridgeContext, httpx.Client, InMemoryPersistenceBackend, list[str], list[int]]:
        """Message 1 done (refreshed, persisted `rt-rotated`); then a foreign same-content
        save moves the mtime. Returns ctx, client, backend, presented refresh tokens and
        the refresh counter; the caller arms the read failure.
        """
        presented: list[str] = []
        transport, refresh_calls = _build_auth_and_mcp_transport(
            _ok_mcp_handler, presented_refresh_tokens=presented
        )
        ctx = _ctx_with_refresh_token_and_transport(
            build_persistence, context_name="prod", transport=transport, warnings=warnings
        )
        client = httpx.Client(transport=transport)
        backend = build_persistence("prod")
        first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        assert first is not None
        assert "result" in first
        backend.save(backend.load())  # foreign rewrite: same content, mtime moves
        return ctx, client, backend, presented, refresh_calls

    def test_changed_mtime_with_minus_67701_read_falls_back_to_last_known_refresh_token(
        self,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        fake_keychain_error: type[FakeKeychainError],
    ) -> None:
        warnings: list[str] = []
        ctx, client, backend, presented, _ = self._primed_ctx_with_foreign_bump(
            build_in_memory_persistence, warnings
        )
        backend.fail_load_with = fake_keychain_error(-67701)
        ctx.access_token_cache.expires_at = 1

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        assert "result" in reply
        assert presented == ["seed-rt", "rt-rotated"]  # refreshed from the in-memory token
        assert len(warnings) == 1
        assert "-67701" in warnings[0]
        assert "rt-rotated" not in warnings[0]

    def test_other_read_errors_do_not_fall_back(
        self,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        fake_keychain_error: type[FakeKeychainError],
    ) -> None:
        warnings: list[str] = []
        ctx, client, backend, _, _ = self._primed_ctx_with_foreign_bump(
            build_in_memory_persistence, warnings
        )
        backend.fail_load_with = fake_keychain_error(-25308)
        ctx.access_token_cache.expires_at = 1

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        error = cast("dict[str, object]", reply["error"])
        assert error["code"] == -32001
        assert "-25308" in cast("str", error["message"])
        assert warnings == []

    def test_persistent_minus_67701_reads_once_then_serves_three_messages_without_rereads(
        self,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        fake_keychain_error: type[FakeKeychainError],
    ) -> None:
        """CHANGES F-4: the fallback records the pre-read mtime, so B and C do not re-read
        and the warning is logged once per fallback event, not per message.
        """
        warnings: list[str] = []
        ctx, client, backend, presented, _ = self._primed_ctx_with_foreign_bump(
            build_in_memory_persistence, warnings
        )
        backend.fail_load_with = fake_keychain_error(-67701)
        loads_before_a = backend.loads
        replies: list[dict[str, object] | None] = []
        loads_after: list[int] = []
        for message_id in (2, 3, 4):
            ctx.access_token_cache.expires_at = 1
            reply, _ = _forward_message(client, _tools_call(message_id), session_id=None, ctx=ctx)
            replies.append(reply)
            loads_after.append(backend.loads)

        assert all(r is not None and "result" in r for r in replies)
        assert loads_after[0] > loads_before_a  # A's retried attempts
        assert loads_after[1] == loads_after[0]
        assert loads_after[2] == loads_after[0]
        assert presented == ["seed-rt", "rt-rotated", "rt-rotated", "rt-rotated"]
        assert len(warnings) == 1


class TestBestEffortPersistAfterRefresh:
    """Issue #181 AC-BI-006: a refresh whose persist fails still serves the call; the
    rotated refresh token lives on in memory.
    """

    def _ctx_with_failing_save(
        self,
        build_persistence: Callable[[str], InMemoryPersistenceBackend],
        warnings: list[str],
    ) -> tuple[_BridgeContext, httpx.Client, InMemoryPersistenceBackend, list[str], list[int]]:
        presented: list[str] = []
        transport, refresh_calls = _build_auth_and_mcp_transport(
            _ok_mcp_handler, presented_refresh_tokens=presented
        )
        ctx = _ctx_with_refresh_token_and_transport(
            build_persistence, context_name="prod", transport=transport, warnings=warnings
        )
        backend = build_persistence("prod")
        backend.fail_save_with = OSError("disk on fire")
        return ctx, httpx.Client(transport=transport), backend, presented, refresh_calls

    def test_refresh_succeeds_but_persist_fails_call_proceeds_and_later_messages_do_not_refresh(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        warnings: list[str] = []
        ctx, client, backend, _, refresh_calls = self._ctx_with_failing_save(
            build_in_memory_persistence, warnings
        )

        first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        loads_after_first = backend.loads  # message 1 does the one unconditional read
        later = [
            _forward_message(client, _tools_call(n), session_id=None, ctx=ctx)[0] for n in (2, 3)
        ]
        replies = [first, *later]

        assert all(r is not None and "result" in r for r in replies)
        assert refresh_calls == [1]
        assert ctx.access_token_cache.token == "refreshed-token-1"
        assert backend.loads == loads_after_first  # unchanged mtime: no re-read either
        assert len(warnings) == 1

    def test_after_persist_failure_expiry_refreshes_from_the_rotated_token_in_memory(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        warnings: list[str] = []
        ctx, client, _, presented, _ = self._ctx_with_failing_save(
            build_in_memory_persistence, warnings
        )
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        ctx.access_token_cache.expires_at = 1

        reply, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)

        assert reply is not None
        assert "result" in reply
        assert presented == ["seed-rt", "rt-rotated"]  # not the stale stored seed

    def test_persist_failure_warning_is_logged_once_not_per_message(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        warnings: list[str] = []
        ctx, client, _, _, _ = self._ctx_with_failing_save(build_in_memory_persistence, warnings)

        for message_id in (1, 2, 3, 4):
            _forward_message(client, _tools_call(message_id), session_id=None, ctx=ctx)

        assert len(warnings) == 1
        assert "rt-rotated" not in warnings[0]
        assert "seed-rt" not in warnings[0]

    def test_foreign_write_of_the_stale_stored_token_keeps_the_newer_in_memory_token(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """CHANGES F-5: the store still holds the old token (persist failed); a foreign
        rewrite of that same stale token must not replace the newer in-memory one.
        """
        warnings: list[str] = []
        ctx, client, backend, presented, refresh_calls = self._ctx_with_failing_save(
            build_in_memory_persistence, warnings
        )
        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
        backend.fail_save_with = None
        backend.save(backend.load())  # foreign write of the stale stored token: mtime moves

        still_cached, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
        ctx.access_token_cache.expires_at = 1
        refreshed, _ = _forward_message(client, _tools_call(3), session_id=None, ctx=ctx)

        assert still_cached is not None
        assert "result" in still_cached
        assert refreshed is not None
        assert "result" in refreshed
        assert refresh_calls == [2]  # message 2 kept its cached access token
        assert presented == ["seed-rt", "rt-rotated"]


class TestBoundedKeychainCall:
    """Issue #181 AC-BI-007: a keychain call that stalls past the bound returns a
    credential-store error to the host within that bound and never wedges the loop.
    """

    _BOUND = 0.2

    def _stall_setup(
        self, build_persistence: Callable[[str], InMemoryPersistenceBackend], warnings: list[str]
    ) -> tuple[_BridgeContext, httpx.Client, InMemoryPersistenceBackend, list[str], list[int]]:
        presented: list[str] = []
        transport, refresh_calls = _build_auth_and_mcp_transport(
            _ok_mcp_handler, presented_refresh_tokens=presented
        )
        ctx = _ctx_with_refresh_token_and_transport(
            build_persistence,
            context_name="prod",
            transport=transport,
            warnings=warnings,
            timeout_seconds=self._BOUND,
        )
        return (
            ctx,
            httpx.Client(transport=transport),
            build_persistence("prod"),
            presented,
            refresh_calls,
        )

    @staticmethod
    def _release_and_join(gates: list[threading.Event], before: set[threading.Thread]) -> None:
        for gate in gates:
            gate.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(timeout=2)

    def test_stalled_keychain_read_returns_store_error_within_bound_and_next_message_processed(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        ctx, client, backend, _, _ = self._stall_setup(build_in_memory_persistence, [])
        gate = threading.Event()
        backend.load_gate = gate
        before = set(threading.enumerate())
        try:
            started = time.monotonic()
            stalled, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
            elapsed = time.monotonic() - started
        finally:
            self._release_and_join([gate], before)

        assert elapsed < 1.0
        assert stalled is not None
        error = cast("dict[str, object]", stalled["error"])
        assert error["code"] == -32001
        assert "credential store" in cast("str", error["message"])
        recovered, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
        assert recovered is not None
        assert "result" in recovered

    def test_message_during_an_abandoned_stall_fails_fast_not_after_another_bound(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        ctx, client, backend, _, _ = self._stall_setup(build_in_memory_persistence, [])
        gate = threading.Event()
        backend.load_gate = gate
        before = set(threading.enumerate())
        threads_before = threading.active_count()
        try:
            _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
            started = time.monotonic()
            second, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
            elapsed = time.monotonic() - started
            threads_during = threading.active_count()
        finally:
            self._release_and_join([gate], before)

        assert elapsed < 0.1
        assert second is not None
        assert cast("dict[str, object]", second["error"])["code"] == -32001
        assert threads_during - threads_before <= 1

    def test_stalled_call_does_not_block_a_message_that_needs_no_keychain_access(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        """CHANGES F-3: message 1's best-effort save stalls (thread abandoned, mtime
        unchanged); message 2 has a valid cache and an unchanged mtime, so it needs no
        keychain call and succeeds while the abandoned thread is still alive.
        """
        warnings: list[str] = []
        ctx, client, backend, _, refresh_calls = self._stall_setup(
            build_in_memory_persistence, warnings
        )
        gate = threading.Event()
        backend.save_gate = gate
        before = set(threading.enumerate())
        try:
            first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
            abandoned_alive = any(t.is_alive() for t in set(threading.enumerate()) - before)
            started = time.monotonic()
            second, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
            elapsed = time.monotonic() - started
        finally:
            self._release_and_join([gate], before)

        assert abandoned_alive
        assert first is not None
        assert "result" in first
        assert second is not None
        assert "result" in second
        assert elapsed < 0.1
        assert refresh_calls == [1]

    def test_stalled_save_during_refresh_persist_is_best_effort(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        warnings: list[str] = []
        ctx, client, backend, presented, _ = self._stall_setup(
            build_in_memory_persistence, warnings
        )
        gate = threading.Event()
        backend.save_gate = gate
        before = set(threading.enumerate())
        try:
            started = time.monotonic()
            first, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
            elapsed = time.monotonic() - started
            ctx.access_token_cache.expires_at = 1
            second, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
        finally:
            self._release_and_join([gate], before)

        assert elapsed < 1.0
        assert first is not None
        assert "result" in first
        assert second is not None
        assert "result" in second
        assert presented == ["seed-rt", "rt-rotated"]  # refreshed from the in-memory token
        assert len(warnings) == 2  # one per failed persist, no token value in either
        assert all("rt-rotated" not in w and "seed-rt" not in w for w in warnings)

    def test_stalled_stat_errors_that_message_only_and_the_next_message_retries(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        ctx, client, backend, _, _ = self._stall_setup(build_in_memory_persistence, [])
        gate = threading.Event()
        backend.mtime_gate = gate
        before = set(threading.enumerate())
        try:
            stalled, _ = _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)
            started = time.monotonic()
            retried, _ = _forward_message(client, _tools_call(2), session_id=None, ctx=ctx)
            retry_elapsed = time.monotonic() - started
        finally:
            self._release_and_join([gate], before)
        recovered, _ = _forward_message(client, _tools_call(3), session_id=None, ctx=ctx)

        assert stalled is not None
        assert cast("dict[str, object]", stalled["error"])["code"] == -32001
        assert retried is not None
        assert "error" in retried
        assert retry_elapsed >= self._BOUND * 0.75  # waited its own bound: not fail-fast
        assert recovered is not None
        assert "result" in recovered

    def test_production_view_construction_uses_the_five_second_bound(self) -> None:
        # AC-BI-007: main() builds the view without `timeout_seconds`, so the
        # constructor's own default IS the shipped bound. Sleeping 5 s is needless.
        assert "timeout_seconds" not in inspect.getsource(main)
        default = inspect.signature(BridgeCredentialView).parameters["timeout_seconds"].default
        assert default == 5.0


def _latency_seconds(logged: str, field: str) -> float | None:
    """Parse `<field>=<n>s` out of a log line; `None` when the field is absent.

    A word boundary before the name keeps `latency` apart from the two prefixed fields.
    """
    match = re.search(rf"\b{field}=([0-9.]+)s", logged)
    return float(match.group(1)) if match else None


class TestSplitLatencyLogging:
    """Issue #181 AC-BI-009: token-resolution latency is logged apart from upstream latency."""

    def test_forwarded_log_line_reports_token_resolution_latency_separately_from_upstream_latency(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        def _slow_handler(_request: httpx.Request) -> httpx.Response:
            time.sleep(0.2)
            return _ok_mcp_handler(_request)

        transport, _ = _build_auth_and_mcp_transport(_slow_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            build_in_memory_persistence, context_name="prod", transport=transport
        )
        log_file = io.StringIO()
        ctx = replace(ctx, log_file=log_file)
        build_in_memory_persistence("prod").load_delay_seconds = 0.1  # slow first keychain read

        _forward_message(
            httpx.Client(transport=transport), _tools_call(1), session_id=None, ctx=ctx
        )

        logged = log_file.getvalue()
        token_resolution = _latency_seconds(logged, "token_resolution_latency")
        upstream = _latency_seconds(logged, "upstream_latency")
        total = _latency_seconds(logged, "latency")
        assert token_resolution is not None
        assert upstream is not None
        assert total is not None
        assert 0.1 <= token_resolution < 0.2
        assert upstream >= 0.2
        assert total >= token_resolution + upstream - 0.002

    def test_auth_failure_log_line_has_token_resolution_latency_and_no_upstream_field(
        self, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
    ) -> None:
        log_file = io.StringIO()
        ctx = _ctx_with_stale_bundle(
            build_in_memory_persistence, context_name="prod", log_file=log_file
        )
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
        )

        _forward_message(client, _tools_call(1), session_id=None, ctx=ctx)

        logged = log_file.getvalue()
        assert "could not get access token" in logged
        assert _latency_seconds(logged, "token_resolution_latency") is not None
        assert _latency_seconds(logged, "latency") is not None
        assert "upstream_latency" not in logged

    @pytest.mark.parametrize("failure", ["ge400", "transport"])
    def test_transport_failure_and_ge400_log_lines_carry_all_three_fields(
        self,
        tmp_path: Path,
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        failure: str,
    ) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            if failure == "transport":
                raise httpx.ConnectError("refused")
            return httpx.Response(503, text="down")

        log_file = io.StringIO()
        ctx = _ctx(tmp_path, build_in_memory_persistence, log_file=log_file)

        _forward_message(
            httpx.Client(transport=httpx.MockTransport(_handler)),
            _tools_call(1),
            session_id=None,
            ctx=ctx,
        )

        logged = log_file.getvalue()
        assert _latency_seconds(logged, "token_resolution_latency") is not None
        assert _latency_seconds(logged, "upstream_latency") is not None
        assert _latency_seconds(logged, "latency") is not None


_RT_SENTINEL = "RT-SENTINEL-9f3a"
_AT_SENTINEL = "AT-SENTINEL-9f3a"


class _LeakRig:
    """The between-message disturbances AC-BI-010's scenarios apply to one fake keychain."""

    def __init__(
        self,
        backend: InMemoryPersistenceBackend,
        foreign: PersistenceCredentialStore,
        keychain_error: type[FakeKeychainError],
    ) -> None:
        self.backend = backend
        self.foreign = foreign
        self.keychain_error = keychain_error
        self.gate = threading.Event()
        self._threads_before = set(threading.enumerate())

    def nothing(self) -> None:
        """No disturbance."""

    def logout(self) -> None:
        """`ps-cli auth logout` from another process."""
        self.foreign.delete_tokens("prod")

    def relogin(self) -> None:
        """`ps-cli auth login` from another process stores a different refresh token."""
        self.foreign.set_tokens(
            "prod", TokenBundle(refresh_token=f"{_RT_SENTINEL}-relogin", issuer=_FAKE_ISSUER)
        )

    def arm_unreadable_record(self) -> None:
        """A foreign write moves the mtime, then every keychain read fails with -67701."""
        self.relogin()
        self.backend.fail_load_with = self.keychain_error(-67701)

    def arm_failing_save(self) -> None:
        """Every save fails, with a backend message that itself embeds the refresh token."""
        self.backend.fail_save_with = OSError(f"disk full while writing {_RT_SENTINEL}")

    def arm_stall(self) -> None:
        """Block the next keychain read until `release_stall`."""
        self.backend.load_gate = self.gate

    def release_stall(self) -> None:
        """Let a stalled (abandoned) keychain call finish and wait for its thread."""
        self.gate.set()
        for thread in set(threading.enumerate()) - self._threads_before:
            thread.join(timeout=2)


# scenario -> (arm before message 1, disturb between messages 1 and 2, keychain bound,
# whether the credential store must still hold the refresh sentinel at the end)
_LEAK_SCENARIOS: dict[str, tuple[str, str, float, bool]] = {
    "success": ("nothing", "nothing", 1.0, True),
    "logout": ("nothing", "logout", 1.0, False),
    "relogin": ("nothing", "relogin", 1.0, True),
    "minus_67701_fallback": ("nothing", "arm_unreadable_record", 1.0, True),
    "persist_failure": ("arm_failing_save", "nothing", 1.0, True),
    "stall_timeout": ("arm_stall", "release_stall", 0.2, True),
    "upstream_401": ("nothing", "nothing", 1.0, True),
}


class TestNoTokenValueAnywhere:
    """Issue #181 AC-BI-010: no access- or refresh-token value in any log line, stderr,
    JSON-RPC reply or file; the refresh token is written only into the credential store.
    """

    @pytest.mark.parametrize("scenario", list(_LEAK_SCENARIOS))
    def test_no_token_value_in_any_log_stderr_reply_or_file_across_all_failure_scenarios(
        self,
        scenario: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
        fake_keychain_error: type[FakeKeychainError],
    ) -> None:
        arm, disturb, bound, rt_stays_in_store = _LEAK_SCENARIOS[scenario]

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            del request
            if scenario == "upstream_401":
                return httpx.Response(
                    401, json={"jsonrpc": "2.0", "error": {"code": -1, "message": "unauthorized"}}
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, _ = _build_auth_and_mcp_transport(
            _mcp_handler,
            expires_in=1,  # below the expiry leeway: every message needs a fresh refresh
            access_token_prefix=_AT_SENTINEL,
            rotated_refresh_token=f"{_RT_SENTINEL}-rotated",
        )
        store = _in_memory_credential_store(build_in_memory_persistence)
        store.set_tokens(
            "prod", TokenBundle(refresh_token=f"{_RT_SENTINEL}-seed", issuer=_FAKE_ISSUER)
        )
        backend = build_in_memory_persistence("prod")
        rig = _LeakRig(backend, store, fake_keychain_error)
        log_file = _open_log_file(tmp_path / "bridge.log")
        assert log_file is not None
        ctx = _BridgeContext(
            mcp_url=_MCP_URL,
            context_name="prod",
            service_url=_SERVICE_URL,
            auth_override=None,
            credential_store=BridgeCredentialView(
                store,
                logger=lambda message: _log(message, log_file=log_file),
                timeout_seconds=bound,
            ),
            access_token_cache=AccessTokenCache(),
            log_file=log_file,
            transport=transport,
        )
        client = httpx.Client(transport=transport)

        replies: list[dict[str, object] | None] = []
        try:
            getattr(rig, arm)()
            replies.append(_forward_message(client, _tools_call(1), session_id=None, ctx=ctx)[0])
            getattr(rig, disturb)()
            replies.append(_forward_message(client, _tools_call(2), session_id=None, ctx=ctx)[0])
        finally:
            rig.release_stall()
            log_file.close()

        backend.fail_load_with = None
        backend.load_gate = None
        saved = backend.load()
        surfaces = {
            "stderr": capsys.readouterr().err,
            "replies": json.dumps(replies),
            **{
                str(path): path.read_text(encoding="utf-8", errors="replace")
                for path in tmp_path.rglob("*")
                if path.is_file()
            },
        }
        assert all(reply is not None for reply in replies)
        for sentinel in (_RT_SENTINEL, _AT_SENTINEL):
            leaked = [name for name, text in surfaces.items() if sentinel in text]
            assert leaked == [], f"{sentinel} leaked into {leaked}"
        assert (_RT_SENTINEL in saved) is rt_stays_in_store


def _write_targets_with_prod_context(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "targets.toml").write_text(
        f'current_context = "prod"\n\n[contexts.prod]\nurl = "{_SERVICE_URL}"\n',
        encoding="utf-8",
    )


def test_main_forced_expiry_with_keychain_read_minus_67701_still_completes_tools_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
    fake_keychain_error: type[FakeKeychainError],
) -> None:
    """Issue #181 AC-BI-005 end to end through `main()`.

    A generator-backed `sys.stdin` (the approved boundary) acts between line 1 and line 2:
    a foreign save moves the credential mtime and the keychain read is armed to fail with
    -67701. Short `expires_in` makes the access token stale at once, so message 2 must
    refresh -- from the in-memory refresh token, since the re-read fails.
    """
    _write_targets_with_prod_context(_config_dir_from_env(monkeypatch))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    foreign = PersistenceCredentialStore(build_persistence=portable_persistence)
    foreign.set_tokens("prod", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
    backend = portable_persistence("prod")
    presented: list[str] = []
    transport, _ = _build_auth_and_mcp_transport(
        _ok_mcp_handler, presented_refresh_tokens=presented, expires_in=1
    )

    def _stdin() -> Iterator[str]:
        yield json.dumps(_tools_call(1)) + "\n"
        # Message 1 is fully processed before the loop pulls the next line.
        foreign.set_tokens("prod", TokenBundle(refresh_token="rt-rotated", issuer=_FAKE_ISSUER))
        backend.fail_load_with = fake_keychain_error(-67701)
        yield json.dumps(_tools_call(2)) + "\n"

    monkeypatch.setattr("sys.stdin", _stdin())

    main(transport=transport)

    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(replies) == 2
    assert all("result" in reply for reply in replies)
    assert presented == ["seed-rt", "rt-rotated"]
    logged = (_config_dir_from_env(monkeypatch) / _LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "-67701" in logged


def test_main_fresh_start_with_keychain_read_minus_67701_replies_with_the_login_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    portable_persistence: Callable[[str], InMemoryPersistenceBackend],
    fake_keychain_error: type[FakeKeychainError],
) -> None:
    """No last-known credential to fall back on: -67701 fails closed with the login hint."""
    _write_targets_with_prod_context(_config_dir_from_env(monkeypatch))
    monkeypatch.delenv("PS_CLI_SERVICE_URL", raising=False)
    PersistenceCredentialStore(build_persistence=portable_persistence).set_tokens(
        "prod", TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER)
    )
    portable_persistence("prod").fail_load_with = fake_keychain_error(-67701)
    transport, _ = _build_auth_and_mcp_transport(_ok_mcp_handler)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_tools_call(1)) + "\n"))

    main(transport=transport)

    (line,) = capsys.readouterr().out.splitlines()
    error = json.loads(line)["error"]
    assert error["code"] == -32001
    assert "ps-cli auth login" in error["message"]
    assert "unlocked" not in error["message"]


def _config_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    value = os.environ.get("PS_CLI_CONFIG_DIR")
    assert value is not None
    return Path(value)


# --- Group 1: Location -------------------------------------------------------------


def test_main_writes_log_file_under_resolve_config_dir_location(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-001: the log file lands at `<config_dir>/mcp-bridge.log`, per resolve_config_dir()."""
    config_dir = tmp_path_factory.mktemp("config-dir")
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    assert (config_dir / _LOG_FILE_NAME).exists()


# --- Group 2: Lifecycle & message logging -------------------------------------------


def test_main_logs_startup_banner_with_pid_ppid_service_url_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002: startup writes one line with PID, parent PID, service URL, context."""
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "https://ps.example.test")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in logged
    assert f"ppid={os.getppid()}" in logged
    assert "service_url=https://ps.example.test" in logged
    assert "context=None" in logged


def test_main_logs_exit_reason_stdin_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-005: a clean stdin EOF logs an exit line before the process returns."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    assert "exiting: stdin closed" in log_path.read_text(encoding="utf-8")


def test_main_logs_unhandled_exception_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006: an unhandled exception in the loop is logged, then still propagates."""
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc": "2.0", "method": "x", "id": 1}\n'))

    def _boom(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        main(transport=httpx.MockTransport(_boom))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert "exiting: unhandled exception" in logged
    assert "boom" in logged


def test_forward_message_logs_success_with_method_id_outcome_latency_session(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-003: a successful forward logs method, id, outcome, latency, session id."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 7, "result": {}},
            headers={"mcp-session-id": "sess-42"},
        )

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, build_in_memory_persistence, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 7},
        session_id=None,
        ctx=ctx,
    )

    assert reply == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert session_id == "sess-42"
    logged = log_file.getvalue()
    assert "method=tools/call" in logged
    assert "id=7" in logged
    assert "outcome=ok" in logged
    assert "latency=" in logged
    assert "session=sess-42" in logged


def test_forward_message_logs_transport_failure_with_latency_and_session(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-004: a transport error is logged (retained + extended) and swallowed."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, build_in_memory_persistence, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/list", "id": 3},
        session_id="sess-1",
        ctx=ctx,
    )

    assert reply is None
    assert session_id == "sess-1"
    logged = log_file.getvalue()
    assert "request to PS Service failed" in logged
    assert "method=tools/list" in logged
    assert "id=3" in logged
    assert "latency=" in logged
    assert "session=sess-1" in logged


def test_forward_message_returns_generic_error_reply_when_body_is_not_jsonrpc_shaped(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-004/007: a non-2xx response whose body isn't the JSON-RPC error shape
    PS Service emits still gets a real reply (not silence) -- with a generic,
    status-code-only message, never the raw body text.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, build_in_memory_persistence, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 9},
        session_id="sess-2",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["id"] == 9
    error = reply["error"]
    assert isinstance(error, dict)
    assert "500" in cast("str", error["message"])
    assert "internal error" not in json.dumps(reply)
    assert session_id == "sess-2"
    logged = log_file.getvalue()
    assert "PS Service returned 500" in logged
    assert "method=tools/call" in logged
    assert "id=9" in logged
    assert "latency=" in logged
    assert "session=sess-2" in logged


def test_forward_message_returns_jsonrpc_error_on_ge400_status_with_upstream_message(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-004: a >=400 PS Service response for a genuine request returns a real
    JSON-RPC error reply carrying PS Service's own message text -- not silence.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "graph unavailable"},
            },
        )

    ctx = _ctx(tmp_path, build_in_memory_persistence)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 11},
        session_id=None,
        ctx=ctx,
    )

    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 11
    error = reply["error"]
    assert isinstance(error, dict)
    assert "graph unavailable" in cast("str", error["message"])


def test_forward_message_ge400_for_a_notification_still_returns_no_reply(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-005: a notification (no `id`) never gets a reply, even on a >=400
    PS Service response -- JSON-RPC 2.0 forbids replying to a notification.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Session not found"},
            },
        )

    ctx = _ctx(tmp_path, build_in_memory_persistence)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        session_id="sess-4",
        ctx=ctx,
    )

    assert reply is None


def test_forward_message_session_not_found_error_distinguishes_expired_session(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """AC-BI-004/006: PS Service's literal "Session not found" (mcp SDK's fixed text
    for an unknown/expired session id -- e.g. after a pod restart wiped in-memory
    session state) is rewrapped into a message that names it as an expired/reset
    session, while still carrying PS Service's own original text.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Session not found"},
            },
        )

    ctx = _ctx(tmp_path, build_in_memory_persistence)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 13},
        session_id="sess-5",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["id"] == 13
    error = reply["error"]
    assert isinstance(error, dict)
    message = cast("str", error["message"])
    assert "Session not found" in message  # AC-BI-004: carries PS Service's own text
    assert "expired" in message.lower() or "restart" in message.lower()  # AC-BI-006


def test_forward_message_ge400_error_never_leaks_bearer_token_or_raw_body(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-007: the >=400 error reply never includes the bearer token or PS
    Service's raw response body -- only the sanitized, extracted message text.
    """
    ctx = _ctx_with_cached_access_token(
        build_in_memory_persistence, context_name="prod", access_token="sk-topsecret-access-token"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-topsecret-access-token"
        return httpx.Response(500, text="stacktrace: /var/secrets/db-password=hunter2")

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 17},
        session_id=None,
        ctx=ctx,
    )

    assert reply is not None
    dumped = json.dumps(reply)
    assert "sk-topsecret-access-token" not in dumped
    assert "hunter2" not in dumped
    assert "/var/secrets" not in dumped


def test_forward_message_logs_token_resolution_failure_with_latency_and_session(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-004: a token-resolution failure (no refresh token stored) is logged.

    Issue #119, AC-BI-005/007: it is no longer swallowed into a silent no-reply --
    `reply`'s own shape is covered separately below.
    """
    log_file = io.StringIO()
    ctx = _ctx_with_stale_bundle(
        build_in_memory_persistence, context_name="prod", log_file=log_file
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 5},
        session_id="sess-3",
        ctx=ctx,
    )

    assert reply is not None
    assert session_id == "sess-3"
    logged = log_file.getvalue()
    assert "could not get access token" in logged
    assert "method=tools/call" in logged
    assert "id=5" in logged
    assert "latency=" in logged
    assert "session=sess-3" in logged


def test_forward_message_returns_jsonrpc_error_on_token_resolution_failure(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """Issue #119, AC-BI-005/006/007: a token-resolution failure returns a real
    JSON-RPC error reply -- not a silent no-reply -- so the MCP host (Claude
    Desktop) surfaces the actual cause instead of a generic timeout. Carries the
    original request's own `id`, and never a token value.
    """
    ctx = _ctx_with_stale_bundle(build_in_memory_persistence, context_name="prod")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 5},
        session_id="sess-3",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 5
    error = reply["error"]
    assert isinstance(error, dict)
    assert "stored credentials could not be refreshed" in cast("str", error["message"])
    assert "ps-cli auth login" in cast("str", error["message"])


def test_forward_message_token_resolution_failure_for_a_notification_still_returns_no_reply(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """Issue #119, AC-BI-005: a notification (no `id`) never gets a reply, even on a
    token failure -- JSON-RPC 2.0 forbids replying to a notification; only the log
    line (asserted above) carries the failure for that case.
    """
    ctx = _ctx_with_stale_bundle(build_in_memory_persistence, context_name="prod")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        session_id=None,
        ctx=ctx,
    )

    assert reply is None


# --- Group 3: Resilience -------------------------------------------------------------


def test_log_file_is_appended_across_restarts_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007: restarting against the same config_dir appends, never truncates."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    transport = httpx.MockTransport(lambda _req: pytest.fail("no message should be sent"))

    main(transport=transport)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(transport=transport)

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert logged.count("starting:") == 2
    assert logged.count("exiting: stdin closed") == 2


def test_open_log_file_failure_falls_back_to_none_and_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008: a log file that can't be opened doesn't raise -- warns and returns None."""
    # A regular file where a directory is expected: `mkdir(parents=True, exist_ok=True)`
    # can't paper over that, unlike a merely-missing parent directory (which it creates).
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    unwritable_path = blocked_parent / _LOG_FILE_NAME

    log_file = _open_log_file(unwritable_path)

    assert log_file is None
    assert "could not open log file" in capsys.readouterr().err


def test_main_still_proxies_when_log_file_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-BI-008: the bridge still starts and proxies messages when logging setup fails."""
    # A regular file where a directory is expected breaks `config_dir / _LOG_FILE_NAME`.
    blocked_config_dir = tmp_path / "blocked-config-dir"
    blocked_config_dir.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(blocked_config_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc": "2.0", "method": "x", "id": 1}\n'))

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    main(transport=httpx.MockTransport(_handler))
    # No assertion beyond "did not raise" -- a logging failure must never break the proxy.


# --- Group 4: Security ----------------------------------------------------------------


def test_forward_message_never_logs_message_params_or_bearer_token(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-009: log lines never include the bearer token or the message body/params."""
    log_file = io.StringIO()
    ctx = _ctx_with_cached_access_token(
        build_in_memory_persistence,
        context_name="prod",
        access_token="sk-topsecret-access-token",
        log_file=log_file,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-topsecret-access-token"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    _forward_message(
        client,
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"secret": "super-sensitive-payload"},
        },
        session_id=None,
        ctx=ctx,
    )

    logged = log_file.getvalue()
    assert "sk-topsecret-access-token" not in logged
    assert "super-sensitive-payload" not in logged


def test_open_log_file_creates_file_with_non_world_readable_permissions(tmp_path: Path) -> None:
    """AC-BI-010: a newly created log file is not group/other readable."""
    path = tmp_path / _LOG_FILE_NAME

    log_file = _open_log_file(path)

    assert log_file is not None
    log_file.close()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0


# --- _log() itself ---------------------------------------------------------------------


def test_log_writes_to_both_stderr_and_log_file(capsys: pytest.CaptureFixture[str]) -> None:
    """`_log` writes the same line to stderr (always) and the log file (when given)."""
    log_file = io.StringIO()

    _log("hello world", log_file=log_file)

    assert "hello world" in capsys.readouterr().err
    assert "hello world" in log_file.getvalue()
