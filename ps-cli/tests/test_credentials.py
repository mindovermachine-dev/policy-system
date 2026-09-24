"""Tests for ps_cli.credentials: `CredentialStore`/`KeyringBackend` Protocols and
`KeyringCredentialStore` (issue #121: keyring-only, in-memory-only access tokens).

Issue #121 drops `FileCredentialStore` entirely (AC-BI-008) -- any keyring backend
exception (not only `keyring.errors.KeyringError`) is caught and raised as an
actionable `PsCliError` instead of falling back to a plaintext file (AC-BI-006/007).
`TokenBundle` shrinks to `refresh_token`/`issuer` only (AC-BI-001) -- `access_token`/
`expires_at` are never persisted; the in-memory-only access token this issue
introduces lives in `device_flow.AccessTokenCache` instead (see `test_device_flow.py`).

Portable fakes (`InMemoryKeyringBackend`/`AlwaysRaisingKeyringBackend`) live in
`conftest.py` (D-121-7), shared across every affected test file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import keyring
import keyring.errors
import pytest

from ps_cli.credentials import (
    CredentialStore,
    KeyringCredentialStore,
    TokenBundle,
    build_credential_store,
)
from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from conftest import AlwaysRaisingKeyringBackend, InMemoryKeyringBackend

_TOKENS = TokenBundle(refresh_token="refresh-tok", issuer="https://issuer.example")


def test_keyring_credential_store_happy_path_round_trips(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """`set_tokens` then `get_tokens` round-trips via the fake backend alone."""
    store = KeyringCredentialStore(keyring_backend=keyring_backend)

    store.set_tokens("dev", _TOKENS)

    assert store.get_tokens("dev") == _TOKENS


def test_keyring_credential_store_persists_only_refresh_token_and_issuer(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """AC-BI-001: only `refresh_token`+`issuer` ever reach the keyring backend's own
    string storage -- verified by inspecting the backend directly (not just through
    `CredentialStore.get_tokens()`), so a bug that leaked a third field into the JSON
    blob would be caught even if `TokenBundle`'s own shape somehow still round-tripped.
    """
    store = KeyringCredentialStore(keyring_backend=keyring_backend)

    store.set_tokens("dev", _TOKENS)

    raw = keyring_backend.get_password("ps-cli", "dev")
    assert raw is not None
    assert '"refresh_token"' in raw
    assert '"issuer"' in raw
    assert '"access_token"' not in raw
    assert '"expires_at"' not in raw


def test_keyring_credential_store_get_returns_none_for_missing_context(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """A context with no stored credential resolves to `None`, not an exception."""
    store = KeyringCredentialStore(keyring_backend=keyring_backend)

    assert store.get_tokens("missing") is None


def test_keyring_credential_store_round_trips_with_no_refresh_token(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """A `TokenBundle` with `refresh_token=None` round-trips (AC-BI-002's fail-closed
    "not-logged-in" shape is representable in the store).
    """
    store = KeyringCredentialStore(keyring_backend=keyring_backend)
    tokens = TokenBundle(refresh_token=None, issuer="https://issuer.example")

    store.set_tokens("dev", tokens)

    assert store.get_tokens("dev") == tokens


def test_keyring_credential_store_isolates_credentials_per_context_name(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """Two different context names can never collide (D11's own isolation guarantee).

    `set_tokens("dev", ...)` then `get_tokens("prod")` must never return `"dev"`'s
    value -- proves the injected fake's own `(service_name, username)` keying, which
    mirrors exactly what the real `keyring_backend.get_password("ps-cli", context)` call
    does. Also this issue's replacement for `test_config_handlers.py`'s now-orphaned
    `config_dir`-isolation test, once `build_credential_store()` stops taking a
    `config_dir` at all (D-121-5): isolation is by context name within one
    `KeyringCredentialStore`, never by `config_dir`.
    """
    store = KeyringCredentialStore(keyring_backend=keyring_backend)

    store.set_tokens("dev", _TOKENS)

    assert store.get_tokens("prod") is None


def test_delete_tokens_removes_entry_then_get_returns_none(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """`delete_tokens` removes a stored entry; a subsequent `get_tokens` returns `None`."""
    store = KeyringCredentialStore(keyring_backend=keyring_backend)
    store.set_tokens("dev", _TOKENS)

    store.delete_tokens("dev")

    assert store.get_tokens("dev") is None


class _PasswordDeleteErrorBackend:
    """A `KeyringBackend` fake whose `delete_password` always raises `PasswordDeleteError`.

    `get_password`/`set_password` never raise -- unused by this fake's one test, present
    only for structural completeness of the `KeyringBackend` protocol.
    """

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return `None` unconditionally; unused by this fake's test."""
        del service_name, username
        return None

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """No-op; unused by this fake's test."""
        del service_name, username, password

    def delete_password(self, service_name: str, username: str) -> None:
        """Unconditionally raise `PasswordDeleteError` -- nothing was stored."""
        del service_name, username
        raise keyring.errors.PasswordDeleteError("nothing stored for this context")


def test_delete_tokens_with_password_delete_error_is_benign_noop() -> None:
    """`PasswordDeleteError` is a benign no-op (D-121-4/AC-BI-010) -- never converted
    into a `PsCliError`, unlike every other exception type.
    """
    store = KeyringCredentialStore(keyring_backend=_PasswordDeleteErrorBackend())

    store.delete_tokens("dev")  # must not raise


def test_get_tokens_with_non_keyring_error_exception_raises_actionable_ps_cli_error(
    always_raising_keyring_backend: AlwaysRaisingKeyringBackend,
) -> None:
    """AC-BI-006/011: a bare `OSError` (not `keyring.errors.KeyringError`) from
    `get_password` is caught and raised as `PsCliError`, naming the context and the
    exception type -- never falling back to a file, never leaking a token value
    (AC-BI-007).
    """
    store = KeyringCredentialStore(keyring_backend=always_raising_keyring_backend)

    with pytest.raises(PsCliError) as excinfo:
        store.get_tokens("dev")

    assert "dev" in excinfo.value.msg
    assert "OSError" in (excinfo.value.hint or "")


def test_set_tokens_with_non_keyring_error_exception_raises_actionable_ps_cli_error(
    always_raising_keyring_backend: AlwaysRaisingKeyringBackend,
) -> None:
    """AC-BI-006/007/008/011: a bare `OSError` from `set_password` raises `PsCliError`;
    the raised message/hint never contain the token value that was being stored.
    """
    store = KeyringCredentialStore(keyring_backend=always_raising_keyring_backend)
    secret_value = "super-secret-refresh-token-should-never-print"
    tokens = TokenBundle(refresh_token=secret_value, issuer="https://issuer.example")

    with pytest.raises(PsCliError) as excinfo:
        store.set_tokens("dev", tokens)

    assert "dev" in excinfo.value.msg
    assert secret_value not in excinfo.value.msg
    assert secret_value not in (excinfo.value.hint or "")
    assert "OSError" in (excinfo.value.hint or "")


def test_delete_tokens_with_non_keyring_error_exception_raises_actionable_ps_cli_error(
    always_raising_keyring_backend: AlwaysRaisingKeyringBackend,
) -> None:
    """AC-BI-006/011: a bare `OSError` from `delete_password` raises `PsCliError` --
    proves the generic `except Exception` clause is evaluated *after* the specific
    `except keyring.errors.PasswordDeleteError` clause, not instead of it (D-121-4).
    """
    store = KeyringCredentialStore(keyring_backend=always_raising_keyring_backend)

    with pytest.raises(PsCliError) as excinfo:
        store.delete_tokens("dev")

    assert "dev" in excinfo.value.msg
    assert "OSError" in (excinfo.value.hint or "")


def test_build_credential_store_wraps_the_real_keyring_module() -> None:
    """`build_credential_store()` wires the production `KeyringCredentialStore`.

    Zero-argument (AC-BI-008: no `config_dir`, no `FileCredentialStore` anywhere in
    the get/set/delete path) -- structural check only, behavior is already covered
    above.
    """
    store: CredentialStore = build_credential_store()

    assert isinstance(store, KeyringCredentialStore)
    assert (
        store._keyring_backend  # pyright: ignore[reportPrivateUsage]  # structural factory-wiring proof, not behavior
        is keyring
    )
