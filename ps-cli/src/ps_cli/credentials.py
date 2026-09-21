"""ps-cli credential storage: keyring-first, `credentials.toml` file fallback.

New module introduced by issue #56 (multi-target config model). `CredentialStore` is the
abstraction every caller (`config_handlers.py`, later slices) depends on. Slice 15 defines
both Protocols and `FileCredentialStore`'s round-trip behavior against `credentials.toml`
(PLAN.md issue #56 §1 D9, D15); Slice 16 adds the fallback-warning requirement (D14) to
`FileCredentialStore`'s three methods. Slices 17-21 add `KeyringCredentialStore`
(composing a `FileCredentialStore` fallback and an injected `KeyringBackend`, D9, D11,
D12) and the `build_credential_store()` factory (D9).

Issue #57 Slice 2 (D-57-1) replaces the opaque-string credential with a structured
`TokenBundle` (`access_token`/`refresh_token`/`expires_at`/`issuer`) throughout:
`CredentialStore`'s `get_credential`/`set_credential`/`delete_credential` are renamed
to `get_tokens`/`set_tokens`/`delete_tokens`; `FileCredentialStore` writes one
`[credentials.<context>]` sub-table per context instead of a single flat `[credentials]`
table; `KeyringCredentialStore` JSON-encodes/decodes `TokenBundle` only at the literal
OS-keyring string boundary (`KeyringBackend.set_password`/`get_password` accept/return a
single opaque string -- there is no way around that for *that* backend specifically), never
exposing the JSON encoding through its own public `CredentialStore`-shaped methods. See
PLAN.md (issue #57) §2 Slice 2, CHANGES.md F7.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import keyring
import keyring.errors

from ps_cli.toml_writer import escape_basic_string

if TYPE_CHECKING:
    from pathlib import Path

_CREDENTIALS_FILE_NAME = "credentials.toml"
_CREDENTIALS_FILE_MODE = 0o600


@dataclass(frozen=True)
class TokenBundle:
    """A structured OAuth token bundle: what `CredentialStore` stores per context.

    `expires_at` is an integer Unix epoch second -- chosen over an ISO string/`datetime`
    to keep TOML serialization to a bare unquoted number, with no timezone handling
    needed. See PLAN.md (issue #57) §2 Slice 2, D-57-1.
    """

    access_token: str
    refresh_token: str | None
    expires_at: int
    issuer: str


class CredentialStore(Protocol):
    """A per-context token-bundle store: get/set/delete, keyed by context name.

    `Protocol` for the interface, matching L2 Common Types Handling's "Use Protocol for
    interfaces" and this repo's own precedent (`PsServiceClientProtocol`,
    `http_client.py:228-254`). See PLAN.md (issue #56) §1 D9; issue #57 D-57-1 for the
    `TokenBundle`-shaped rename from `get_credential`/`set_credential`/`delete_credential`.
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

    Lets `KeyringCredentialStore` (a later slice) take its backend as a constructor
    parameter -- the real `keyring` module by default, a hand-written in-memory fake in
    tests -- rather than importing `keyring` directly. See PLAN.md (issue #56) §1 D9.
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


def _parse_token_bundle(table: dict[str, object]) -> TokenBundle:
    """Parse one `[credentials.<context>]` table into a `TokenBundle`.

    `credentials.toml`'s only producer is `FileCredentialStore._write()`, which always
    emits this exact shape -- matches `targets.py::load_targets()`'s identical
    trusted-shape `cast` rationale.
    """
    return TokenBundle(
        access_token=cast("str", table["access_token"]),
        refresh_token=cast("str | None", table.get("refresh_token")),
        expires_at=cast("int", table["expires_at"]),
        issuer=cast("str", table["issuer"]),
    )


def _format_token_bundle_table(context: str, tokens: TokenBundle) -> str:
    """Render one `[credentials.<context>]` table for `tokens`.

    Hand-rolled inline, not via a shared generic nested-table writer -- see
    PLAN.md (issue #57) Slice 1's DRY-threshold citation (`http_client.py:289-291`):
    only two nested-table call sites exist in this codebase (this one, and
    `targets.py::write_targets()`'s `[contexts.<name>]`/`[contexts.<name>.auth]`), under
    this codebase's own stated "extract... once a pattern repeats a third time"
    threshold.
    """
    lines = [
        f"[credentials.{context}]",
        f'access_token = "{escape_basic_string(tokens.access_token)}"',
        f'issuer = "{escape_basic_string(tokens.issuer)}"',
        f"expires_at = {tokens.expires_at}",
    ]
    if tokens.refresh_token is not None:
        lines.append(f'refresh_token = "{escape_basic_string(tokens.refresh_token)}"')
    return "\n".join(lines) + "\n"


class FileCredentialStore:
    """`credentials.toml`-backed `CredentialStore`: the fallback when no OS keyring works.

    Written via `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` +
    `os.fdopen(fd, "w")`, not "write then `chmod`" -- this closes the race window where
    the file would otherwise briefly exist at the process's default (umask-determined)
    permissions before being tightened, per L1 Security by Design's least privilege. See
    PLAN.md (issue #56) §1 D15.

    Every call to `get_tokens`/`set_tokens`/`delete_tokens` prints a warning to stderr
    naming the resolved `credentials.toml` path -- **never** a token value (AC-BI-015) --
    unconditionally on every call, not cached or "once per process" (AC-BI-011's literal
    "every use" wording). `FileCredentialStore` itself has no notion of "is this the
    fallback path" -- it always warns when invoked directly, which is correct whether
    reached via `KeyringCredentialStore`'s fallback branch (a later slice) or exercised
    directly, as this class's own tests do. See PLAN.md (issue #56) §1 D14.

    Issue #57 Slice 2 (D-57-1): stores one `[credentials.<context>]` sub-table per
    context (four keys each) instead of a single flat `[credentials]` table of opaque
    strings.
    """

    def __init__(self, config_dir: Path) -> None:
        """Store `config_dir`; the credentials file itself is `<config_dir>/credentials.toml`."""
        self._config_dir = config_dir
        self._path = config_dir / _CREDENTIALS_FILE_NAME

    def _load(self) -> dict[str, TokenBundle]:
        """Read and parse `credentials.toml`; return `{}` if it does not exist yet."""
        if not self._path.is_file():
            return {}
        raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        raw_credentials = cast("dict[str, dict[str, object]]", raw.get("credentials", {}))
        return {context: _parse_token_bundle(table) for context, table in raw_credentials.items()}

    def _write(self, tokens: dict[str, TokenBundle]) -> None:
        """Serialize `tokens` to `<config_dir>/credentials.toml` with mode 0600."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        content = "\n".join(
            _format_token_bundle_table(context, tokens[context]) for context in sorted(tokens)
        )
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            _CREDENTIALS_FILE_MODE,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _warn_fallback(self) -> None:
        """Print the fallback warning to stderr, naming the path -- never a token value.

        Called unconditionally at the start of every public method (PLAN.md (issue #56)
        §1 D14) -- AC-BI-011 requires the warning on every use, not once per process; and
        AC-BI-015 requires the path, never a token value, to appear in it.
        """
        print(
            f"⚠️  no OS keyring backend available; using {self._path} instead (mode 0600). "
            "This is less secure than an OS keyring.",
            file=sys.stderr,
        )

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored."""
        self._warn_fallback()
        return self._load().get(context)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, creating/overwriting `credentials.toml`."""
        self._warn_fallback()
        all_tokens = self._load()
        all_tokens[context] = tokens
        self._write(all_tokens)

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle, if any; a no-op if none exists."""
        self._warn_fallback()
        all_tokens = self._load()
        if context in all_tokens:
            del all_tokens[context]
            self._write(all_tokens)


_KEYRING_SERVICE_NAME = "ps-cli"


def _encode_token_bundle(tokens: TokenBundle) -> str:
    """JSON-encode `tokens` into the single opaque string the OS keyring API accepts.

    This is the *only* place a `TokenBundle` is JSON-encoded -- the real OS keyring API
    (`KeyringBackend.set_password(service, username, password: str)`) only ever stores
    one opaque string, so there is no way around encoding at *this* specific boundary.
    `KeyringCredentialStore`'s own public methods never expose this encoding to callers
    -- they take/return a structured `TokenBundle`, never this raw string. See PLAN.md
    (issue #57) §2 Slice 2, D-57-1.
    """
    return json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "issuer": tokens.issuer,
        }
    )


def _decode_token_bundle(raw: str) -> TokenBundle:
    """Decode a `TokenBundle` from the opaque string `_encode_token_bundle()` produced.

    Trusted-shape parse (this module's own `_encode_token_bundle()` is the only
    producer of a string ever passed here) -- matches this module's other trusted-shape
    `cast` usages.
    """
    decoded = cast("dict[str, object]", json.loads(raw))
    return TokenBundle(
        access_token=cast("str", decoded["access_token"]),
        refresh_token=cast("str | None", decoded["refresh_token"]),
        expires_at=cast("int", decoded["expires_at"]),
        issuer=cast("str", decoded["issuer"]),
    )


class KeyringCredentialStore:
    """Keyring-first `CredentialStore`: an injected `KeyringBackend`, file fallback.

    Composition, not inheritance (L1 Composition Over Inheritance) -- *has a*
    `FileCredentialStore`, not *is a* one. `keyring_backend` is itself constructor-injected
    (the real `keyring` module by default via `build_credential_store()`, a hand-written
    in-memory fake in tests) rather than imported directly, mirroring the existing
    constructor-injection seam `PsServiceClient`'s `transport` parameter and `cli.run()`'s
    `client` parameter already use. See PLAN.md (issue #56) §1 D9.

    Keys every OS keyring lookup as `(service_name="ps-cli", username=context)` (D11) --
    two different context names can never collide in the OS keyring, by construction
    (AC-BI-013). See D11 for the accepted, flagged scope limitation (no
    `PS_CLI_CONFIG_DIR`-namespacing of the keyring key).
    """

    def __init__(self, *, fallback: FileCredentialStore, keyring_backend: KeyringBackend) -> None:
        """Store the `FileCredentialStore` fallback and the injected `KeyringBackend`."""
        self._fallback = fallback
        self._keyring_backend = keyring_backend

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return `context`'s `TokenBundle` from the keyring, or the file fallback on error.

        Any `keyring.errors.KeyringError` (no backend, locked, init failure) is treated as
        "the backend is unusable" and falls back (D12). The keyring's own opaque string is
        JSON-decoded back into a `TokenBundle` here -- callers never see the raw string.
        """
        try:
            raw = self._keyring_backend.get_password(_KEYRING_SERVICE_NAME, context)
        except keyring.errors.KeyringError:
            return self._fallback.get_tokens(context)
        if raw is None:
            return None
        return _decode_token_bundle(raw)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context` in the keyring, or the file fallback on error.

        Any `keyring.errors.KeyringError` -- including `PasswordSetError`, this specific
        write failing -- falls back (D12). `tokens` is JSON-encoded only for the keyring
        call itself.
        """
        try:
            self._keyring_backend.set_password(
                _KEYRING_SERVICE_NAME, context, _encode_token_bundle(tokens)
            )
        except keyring.errors.KeyringError:
            self._fallback.set_tokens(context, tokens)

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle from the keyring.

        `PasswordDeleteError` alone means the backend works fine but nothing was stored
        for this context -- a benign no-op, **not** a fallback trigger: no fallback call,
        no warning, no file touched. Any other `KeyringError` (`NoKeyringError`,
        `InitError`, `KeyringLocked`) means the backend is genuinely unusable and falls
        back. This distinction matters because D13's `set-context` mechanism calls
        `delete_tokens` unconditionally, including for every brand-new context that
        never had a credential set -- getting it wrong would spuriously fall back (and
        print a misleading warning) on every healthy-keyring `set-context` call. See D12.
        """
        try:
            self._keyring_backend.delete_password(_KEYRING_SERVICE_NAME, context)
        except keyring.errors.PasswordDeleteError:
            return
        except keyring.errors.KeyringError:
            self._fallback.delete_tokens(context)


def build_credential_store(config_dir: Path) -> CredentialStore:
    """Build the default `CredentialStore`: the real `keyring` module, file fallback.

    Composes `KeyringCredentialStore` with a `FileCredentialStore(config_dir)` fallback
    and the real `keyring` module as its backend -- the production wiring every CLI
    command uses. See PLAN.md (issue #56) §1 D9.
    """
    return KeyringCredentialStore(fallback=FileCredentialStore(config_dir), keyring_backend=keyring)
