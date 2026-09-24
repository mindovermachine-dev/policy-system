"""ps-cli credential storage: keyring-only, in-memory-only access tokens (issue #121).

New module introduced by issue #56 (multi-target config model). `CredentialStore` is the
abstraction every caller (`config_handlers.py`, later slices) depends on. Slice 15 defines
both Protocols and `FileCredentialStore`'s round-trip behavior against `credentials.toml`
(PLAN.md issue #56 §1 D9, D15); Slice 16 adds the fallback-warning requirement (D14) to
`FileCredentialStore`'s three methods. Slices 17-21 add `KeyringCredentialStore`
(composing a `FileCredentialStore` fallback and an injected `KeyringBackend`, D9, D11,
D12) and the `build_credential_store()` factory (D9).

Issue #57 Slice 2 (D-57-1) replaces the opaque-string credential with a structured
`TokenBundle` (`access_token`/`refresh_token`/`expires_at`/`issuer`) throughout.

Issue #121: `ps-cli auth login` was hard-crashing on Windows because the combined
access+refresh token JSON could exceed Windows Credential Manager's per-entry blob
limit, and the resulting raw `win32ctypes.pywin32.pywintypes.error` (not a
`keyring.errors.KeyringError`) never triggered the old file fallback -- which was
itself unsafe on Windows and plaintext everywhere. This issue's fix, applied here:

- `TokenBundle` shrinks to `refresh_token`/`issuer` only (AC-BI-001) -- the access
  token is never persisted; it lives in `device_flow.AccessTokenCache` instead,
  in-memory only, for the lifetime of one CLI invocation (AC-BI-003/004).
- `FileCredentialStore` is deleted entirely (D-121-5) -- there is no fallback left to
  reach for. `KeyringCredentialStore` drops its `fallback` constructor parameter.
- `KeyringCredentialStore.get_tokens`/`set_tokens`/`delete_tokens` widen their
  `except` clauses from `keyring.errors.KeyringError` to bare `Exception` (D-121-4):
  any keyring backend exception, not only a `keyring.errors.*` subclass, is caught
  and raised as an actionable `PsCliError` (AC-BI-006/007) -- `delete_tokens` keeps
  its existing `except keyring.errors.PasswordDeleteError: return` clause *before*
  the generic one, since that specific exception alone means "the backend works,
  nothing was stored" (AC-BI-010's benign no-op).
- `build_credential_store()` becomes zero-argument (AC-BI-008): no `config_dir`, no
  `FileCredentialStore` anywhere in the get/set/delete path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast

import keyring
import keyring.errors

from ps_cli.errors import PsCliError


@dataclass(frozen=True)
class TokenBundle:
    """The only two fields `CredentialStore` ever persists per context (issue #121).

    `access_token`/`expires_at` are never part of this shape (AC-BI-001) -- the
    in-memory-only access token this issue introduces lives in
    `device_flow.AccessTokenCache` instead, never here. See PLAN.md (issue #121)
    D-121-1.
    """

    refresh_token: str | None
    issuer: str


class CredentialStore(Protocol):
    """A per-context token-bundle store: get/set/delete, keyed by context name.

    `Protocol` for the interface, matching L2 Common Types Handling's "Use Protocol for
    interfaces" and this repo's own precedent (`PsServiceClientProtocol`,
    `http_client.py:228-254`). See PLAN.md (issue #56) §1 D9; issue #57 D-57-1 for the
    `TokenBundle`-shaped rename from `get_credential`/`set_credential`/`delete_credential`;
    issue #121 D-121-1 for the shrunk `TokenBundle` shape.
    """

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored."""
        ...

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing value."""
        ...

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op if none exists."""
        ...


class KeyringBackend(Protocol):
    """Structural interface matching the subset of the `keyring` module's API this uses.

    Lets `KeyringCredentialStore` take its backend as a constructor parameter -- the
    real `keyring` module by default, a hand-written in-memory fake in tests -- rather
    than importing `keyring` directly. See PLAN.md (issue #56) §1 D9.
    """

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return the stored password for `(service_name, username)`, or `None`."""
        ...

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Store `password` for `(service_name, username)`."""
        ...

    def delete_password(self, service_name: str, username: str) -> None:
        """Remove the stored password for `(service_name, username)`."""
        ...


_KEYRING_SERVICE_NAME = "ps-cli"


def _encode_token_bundle(tokens: TokenBundle) -> str:
    """JSON-encode `tokens` into the single opaque string the OS keyring API accepts.

    This is the *only* place a `TokenBundle` is JSON-encoded -- the real OS keyring API
    (`KeyringBackend.set_password(service, username, password: str)`) only ever stores
    one opaque string, so there is no way around encoding at *this* specific boundary.
    `KeyringCredentialStore`'s own public methods never expose this encoding to callers
    -- they take/return a structured `TokenBundle`, never this raw string. See PLAN.md
    (issue #57) §2 Slice 2, D-57-1; issue #121 D-121-1 for the shrunk two-field shape.
    """
    return json.dumps({"refresh_token": tokens.refresh_token, "issuer": tokens.issuer})


def _decode_token_bundle(raw: str) -> TokenBundle:
    """Decode a `TokenBundle` from the opaque string `_encode_token_bundle()` produced.

    Trusted-shape parse (this module's own `_encode_token_bundle()` is the only
    producer of a string ever passed here) -- matches this module's other trusted-shape
    `cast` usages.
    """
    decoded = cast("dict[str, object]", json.loads(raw))
    return TokenBundle(
        refresh_token=cast("str | None", decoded["refresh_token"]),
        issuer=cast("str", decoded["issuer"]),
    )


class KeyringCredentialStore:
    """Keyring-only `CredentialStore`: an injected `KeyringBackend`, no fallback (issue #121).

    `keyring_backend` is constructor-injected (the real `keyring` module by default via
    `build_credential_store()`, a hand-written in-memory fake in tests) rather than
    imported directly, mirroring the existing constructor-injection seam
    `PsServiceClient`'s `transport` parameter and `cli.run()`'s `client` parameter
    already use. See PLAN.md (issue #56) §1 D9.

    Keys every OS keyring lookup as `(service_name="ps-cli", username=context)` (D11) --
    two different context names can never collide in the OS keyring, by construction.

    Issue #121, D-121-5: there is no `fallback` any more -- any keyring backend
    exception is raised as an actionable `PsCliError` instead (AC-BI-006/007/008).
    """

    def __init__(self, *, keyring_backend: KeyringBackend) -> None:
        """Store the injected `KeyringBackend`."""
        self._keyring_backend = keyring_backend

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return `context`'s `TokenBundle` from the keyring, or `None` if none is stored.

        Any exception from the backend -- not only `keyring.errors.KeyringError`
        (AC-BI-006: the real Windows failure is a raw
        `win32ctypes.pywin32.pywintypes.error`) -- is caught and raised as an
        actionable `PsCliError`; there is no fallback to fall back to any more
        (AC-BI-007/008). The keyring's own opaque string is JSON-decoded back into a
        `TokenBundle` here -- callers never see the raw string.
        """
        try:
            raw = self._keyring_backend.get_password(_KEYRING_SERVICE_NAME, context)
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the OS keyring for context '{context}'",
                hint=f"the OS keyring backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc
        if raw is None:
            return None
        return _decode_token_bundle(raw)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context` in the keyring.

        Any backend exception is caught and raised as an actionable `PsCliError`
        (AC-BI-006/007/008) -- never falls back to writing a plaintext file.
        `tokens` is JSON-encoded only for the keyring call itself.
        """
        try:
            self._keyring_backend.set_password(
                _KEYRING_SERVICE_NAME, context, _encode_token_bundle(tokens)
            )
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the OS keyring for context '{context}'",
                hint=f"the OS keyring backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle from the keyring.

        `PasswordDeleteError` alone means the backend works fine but nothing was
        stored for this context -- a benign no-op (AC-BI-010), evaluated *before* the
        generic `Exception` clause below (D-121-4: Python evaluates `except` clauses
        in order, and `PasswordDeleteError` is a subclass of `Exception`, so the more
        specific clause correctly wins first). Any other exception means the backend
        is genuinely unusable and is raised as an actionable `PsCliError`
        (AC-BI-006/007).
        """
        try:
            self._keyring_backend.delete_password(_KEYRING_SERVICE_NAME, context)
        except keyring.errors.PasswordDeleteError:
            return
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the OS keyring for context '{context}'",
                hint=f"the OS keyring backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc


def build_credential_store() -> CredentialStore:
    """Build the default `CredentialStore`: the real `keyring` module, no fallback.

    Issue #121 (AC-BI-008): `FileCredentialStore` is gone entirely -- any keyring
    failure now raises `PsCliError` instead of silently falling back to a plaintext
    file. Zero-argument: there is no `config_dir`-scoped fallback left to construct
    (`KeyringCredentialStore` keys the OS keyring only by `(service_name, context)`,
    D11, unaffected by `config_dir`).
    """
    return KeyringCredentialStore(keyring_backend=keyring)
