"""ps-cli credential storage: msal-extensions-backed, in-memory-only access tokens.

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
limit, and the resulting raw `win32ctypes.pywin32.pywintypes.error` (not the OS-
credential-store library's own reshaped error type) never triggered the old file
fallback -- which was itself unsafe on Windows and plaintext everywhere. This issue's
fix, applied here:

- `TokenBundle` shrinks to `refresh_token`/`issuer` only (AC-BI-001) -- the access
  token is never persisted; it lives in `device_flow.AccessTokenCache` instead,
  in-memory only, for the lifetime of one CLI invocation (AC-BI-003/004).
- `FileCredentialStore` is deleted entirely (D-121-5) -- there is no fallback left to
  reach for. `KeyringCredentialStore` drops its `fallback` constructor parameter.
- `KeyringCredentialStore.get_tokens`/`set_tokens`/`delete_tokens` widen their
  `except` clauses from the OS-credential-store library's own error type to bare
  `Exception` (D-121-4): any backend exception, not only that library's own error
  hierarchy, is caught and raised as an actionable `PsCliError` (AC-BI-006/007) --
  `delete_tokens` keeps its existing early-return clause for that library's specific
  "delete of nothing stored" exception *before* the generic one, since that specific
  exception alone means "the backend works, nothing was stored" (AC-BI-010's benign
  no-op).
- `build_credential_store()` becomes zero-argument (AC-BI-008): no `config_dir`, no
  `FileCredentialStore` anywhere in the get/set/delete path.

Issue #123: the OS-credential-store dependency used through #121 is replaced outright
by `msal-extensions` (Windows Credential Manager's ~1280-char per-entry blob limit was
still being hit even after #121's `TokenBundle` shrink, on accounts with unusually
large refresh tokens). Slice 1: `KeyringBackend` -> `PersistenceBackend` (D-123-1),
matching msal-extensions' own `BasePersistence` shape (`save`/`load`/`get_location`,
no `username` parameter -- one persistence instance is bound to one on-disk location,
not a `(service, username)` key pair). `KeyringCredentialStore` -> `PersistenceCredentialStore`
(D-123-2): the constructor now takes a per-context persistence *factory*
(`Callable[[str], PersistenceBackend]`), called once per `get_tokens`/`set_tokens`/
`delete_tokens(context)` invocation, rather than one shared backend instance -- a
`BasePersistence` object cannot itself be keyed by context. Every `save`/`load` call is
wrapped in `msal_extensions.CrossPlatLock` (D-123-6) to avoid a lost-update race between
two concurrent `ps-cli` invocations against the same context. There is no `delete`
primitive in msal-extensions' public API (no backend supports it) -- `delete_tokens`
overwrites with an empty string instead, and `get_tokens` treats an empty loaded string
the same as `PersistenceNotFound` (D-123-4): both mean "nothing stored for this
context". Slice 1 wired a not-yet-implemented placeholder for
`build_credential_store()`'s real production wiring; Slice 4 (this change) replaces it
with the real `msal_extensions.build_encrypted_persistence`-backed factory,
`resolve_config_dir()`-scoped locations per D-123-7, and AC-BI-009's Linux
`ImportError`/`ValueError` translation. Test code hooks
`_build_production_persistence` via `monkeypatch` (`conftest.py`'s
`portable_persistence`/`unusable_persistence`) exactly as it previously hooked the old
OS-credential-store library's free functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import msal_extensions
import msal_extensions.persistence

from ps_cli.errors import PsCliError
from ps_cli.targets import resolve_config_dir

if TYPE_CHECKING:
    from collections.abc import Callable


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


class PersistenceBackend(Protocol):
    """Structural interface matching msal-extensions' own `BasePersistence` shape.

    Lets `PersistenceCredentialStore` take its per-context persistence object as an
    injected value (via a factory, see `PersistenceCredentialStore`'s own docstring)
    rather than importing `msal_extensions` directly at every call site. Deliberately
    does not reshape `PersistenceNotFound` into a module-local exception type --
    Python's typing system cannot encode "raises X" in a `Protocol`, so callers import
    `msal_extensions.persistence.PersistenceNotFound` directly, exactly how this module
    previously imported the old OS-credential-store library's own delete-error type
    directly rather than reshaping it. Issue #123, D-123-1.
    """

    def save(self, content: str) -> None:
        """Save `content` into this persistence, overwriting any existing content."""
        ...

    def load(self) -> str:
        """Return this persistence's stored content.

        Raises `msal_extensions.persistence.PersistenceNotFound` if `save()` was never
        called for this location.
        """
        ...

    def get_location(self) -> str:
        """Return the on-disk path this persistence instance stores (meta)data into."""
        ...


def _encode_token_bundle(tokens: TokenBundle) -> str:
    """JSON-encode `tokens` into the single opaque string a persistence backend accepts.

    This is the *only* place a `TokenBundle` is JSON-encoded -- `PersistenceBackend.save`
    only ever stores one opaque string, so there is no way around encoding at *this*
    specific boundary. `PersistenceCredentialStore`'s own public methods never expose
    this encoding to callers -- they take/return a structured `TokenBundle`, never this
    raw string. See PLAN.md (issue #57) §2 Slice 2, D-57-1; issue #121 D-121-1 for the
    shrunk two-field shape.
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


class PersistenceCredentialStore:
    """Msal-extensions-backed `CredentialStore`: an injected persistence factory (issue #123).

    `build_persistence` is constructor-injected (the real production factory by
    default via `build_credential_store()`, a hand-written in-memory fake in tests)
    rather than importing `msal_extensions` directly, mirroring the existing
    constructor-injection seam `PsServiceClient`'s `transport` parameter and
    `cli.run()`'s `client` parameter already use. See PLAN.md (issue #56) §1 D9.

    Unlike the old `KeyringCredentialStore`, `build_persistence` is a *factory*
    (`Callable[[str], PersistenceBackend]`), called once per method invocation to
    obtain that context's own persistence object -- a `BasePersistence` instance is
    bound to exactly one on-disk location, so it cannot itself be shared across
    contexts the way one `KeyringBackend` instance could (issue #123, D-123-2).

    There is no `delete` primitive anywhere in msal-extensions' public API --
    `delete_tokens` overwrites with an empty string instead, and `get_tokens` treats
    an empty loaded string the same as "nothing stored" (D-123-4).
    """

    def __init__(self, *, build_persistence: Callable[[str], PersistenceBackend]) -> None:
        """Store the injected per-context persistence factory (D-123-2)."""
        self._build_persistence = build_persistence

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return `context`'s `TokenBundle`, or `None` if none is stored (D-123-4).

        Any exception from the backend other than `PersistenceNotFound` -- not only a
        `msal_extensions`-specific type (AC-BI-006: the real Windows/Linux failures
        are OS-level exceptions, not a reshaped library type) -- is caught and raised
        as an actionable `PsCliError`; there is no fallback to fall back to any more
        (AC-BI-007/008). The persistence backend's own opaque string is JSON-decoded
        back into a `TokenBundle` here -- callers never see the raw string.
        """
        persistence = self._build_persistence(context)
        try:
            with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                raw = persistence.load()
        except msal_extensions.persistence.PersistenceNotFound:
            return None
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the credential store for context '{context}'",
                hint=f"the credential-storage backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc
        if raw == "":  # D-123-4: delete's empty-sentinel means "nothing stored"
            return None
        return _decode_token_bundle(raw)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context` (AC-BI-006/007/008).

        Any backend exception is caught and raised as an actionable `PsCliError` --
        never falls back to writing a plaintext file. `tokens` is JSON-encoded only
        for the persistence call itself.
        """
        persistence = self._build_persistence(context)
        try:
            with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                persistence.save(_encode_token_bundle(tokens))
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the credential store for context '{context}'",
                hint=f"the credential-storage backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op-safe overwrite (D-123-4).

        Unconditionally overwrites with an empty string -- no msal-extensions backend
        distinguishes "overwriting existing content" from "writing for the first
        time", so there is no `PasswordDeleteError`-equivalent benign-no-op special
        case any more (AC-BI-006/010): any exception here means the backend is
        genuinely unusable.
        """
        persistence = self._build_persistence(context)
        try:
            with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                persistence.save("")
        except Exception as exc:
            raise PsCliError(
                msg=f"could not access the credential store for context '{context}'",
                hint=f"the credential-storage backend raised {type(exc).__name__}; "
                "check that it is available and unlocked",
            ) from exc


def _build_production_persistence(context: str) -> PersistenceBackend:
    """The production persistence factory `build_credential_store()` wires in.

    Real `msal_extensions.build_encrypted_persistence`-backed factory (D-123-7's
    location scheme: `resolve_config_dir() / "credentials" / f"{context}.bin"`).
    Named as a module-level function, rather than inlined into
    `build_credential_store()`, precisely so it can be identity-checked and
    monkeypatched without changing `build_credential_store()`'s own zero-argument
    signature or its 9 existing call sites (D-123-8).

    On Linux, `build_encrypted_persistence` requires `python3-gi`/`gir1.2-secret-1`
    (the OS Secret Service bindings) to be installed; when they are missing it raises
    `ImportError` or `ValueError` rather than silently falling back to plaintext
    (D-123-5 -- there is no plaintext fallback anywhere in this seam, AC-BI-010). Both
    are reshaped into an actionable `PsCliError` (AC-BI-009) whose hint is the
    library's own exception message verbatim -- that message already names the exact
    `apt install ...` command, so surfacing it unmodified keeps this correct even if a
    future msal-extensions release changes its own package name or command text.
    """
    location = str(resolve_config_dir() / "credentials" / f"{context}.bin")
    try:
        return cast(
            "PersistenceBackend",
            msal_extensions.build_encrypted_persistence(  # pyright: ignore[reportUnknownMemberType]  # msal-extensions: no py.typed (§1.5)
                location
            ),
        )
    except (ImportError, ValueError) as exc:
        raise PsCliError(
            msg="the Linux credential-storage backend is missing a required system package",
            hint=str(exc),
        ) from exc


def build_credential_store() -> CredentialStore:
    """Build the default `CredentialStore`: `PersistenceCredentialStore`, no fallback.

    Issue #121 (AC-BI-008): any storage failure now raises `PsCliError` instead of
    silently falling back to a plaintext file. Issue #123: the injected backend is now
    an msal-extensions-backed persistence factory rather than the previous OS-
    credential-store dependency. Zero-argument: `_build_production_persistence`
    resolves its own location internally
    (D-123-8), mirroring every other zero-argument call site in this codebase that
    already independently calls `resolve_config_dir()`.
    """
    return PersistenceCredentialStore(build_persistence=_build_production_persistence)
