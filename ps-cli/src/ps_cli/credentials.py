"""ps-cli credential storage: keyring-first, `credentials.toml` file fallback.

New module introduced by issue #56 (multi-target config model). `CredentialStore` is the
abstraction every caller (`config_handlers.py`, later slices) depends on. Slice 15 defines
both Protocols and `FileCredentialStore`'s round-trip behavior against `credentials.toml`
(PLAN.md issue #56 §1 D9, D15); Slice 16 adds the fallback-warning requirement (D14) to
`FileCredentialStore`'s three methods. Slices 17-21 add `KeyringCredentialStore`
(composing a `FileCredentialStore` fallback and an injected `KeyringBackend`, D9, D11,
D12) and the `build_credential_store()` factory (D9).
"""

from __future__ import annotations

import os
import sys
import tomllib
from typing import TYPE_CHECKING, Protocol, cast

import keyring
import keyring.errors

from ps_cli.toml_writer import format_flat_table

if TYPE_CHECKING:
    from pathlib import Path

_CREDENTIALS_FILE_NAME = "credentials.toml"
_CREDENTIALS_FILE_MODE = 0o600


class CredentialStore(Protocol):
    """A per-context credential store: get/set/delete, keyed by context name.

    `Protocol` for the interface, matching L2 Common Types Handling's "Use Protocol for
    interfaces" and this repo's own precedent (`PsServiceClientProtocol`,
    `http_client.py:228-254`). See PLAN.md (issue #56) §1 D9.
    """

    def get_credential(self, context: str) -> str | None:
        """Return the stored credential for `context`, or `None` if none is stored."""
        ...

    def set_credential(self, context: str, credential: str) -> None:
        """Store `credential` for `context`, overwriting any existing value."""
        ...

    def delete_credential(self, context: str) -> None:
        """Remove `context`'s stored credential; a no-op if none exists."""
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


class FileCredentialStore:
    """`credentials.toml`-backed `CredentialStore`: the fallback when no OS keyring works.

    Written via `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` +
    `os.fdopen(fd, "w")`, not "write then `chmod`" -- this closes the race window where
    the file would otherwise briefly exist at the process's default (umask-determined)
    permissions before being tightened, per L1 Security by Design's least privilege. See
    PLAN.md (issue #56) §1 D15.

    Every call to `get_credential`/`set_credential`/`delete_credential` prints a warning
    to stderr naming the resolved `credentials.toml` path -- **never** the credential
    value (AC-BI-015) -- unconditionally on every call, not cached or "once per process"
    (AC-BI-011's literal "every use" wording). `FileCredentialStore` itself has no notion
    of "is this the fallback path" -- it always warns when invoked directly, which is
    correct whether reached via `KeyringCredentialStore`'s fallback branch (a later
    slice) or exercised directly, as this class's own tests do. See PLAN.md (issue #56)
    §1 D14.
    """

    def __init__(self, config_dir: Path) -> None:
        """Store `config_dir`; the credentials file itself is `<config_dir>/credentials.toml`."""
        self._config_dir = config_dir
        self._path = config_dir / _CREDENTIALS_FILE_NAME

    def _load(self) -> dict[str, str]:
        """Read and parse `credentials.toml`; return `{}` if it does not exist yet."""
        if not self._path.is_file():
            return {}
        raw = tomllib.loads(self._path.read_text(encoding="utf-8"))
        # `credentials.toml`'s only producer is this class's own `_write()`, which always
        # emits this exact shape -- matches `targets.py::load_targets()`'s identical
        # trusted-shape `cast` rationale.
        return cast("dict[str, str]", raw.get("credentials", {}))

    def _write(self, credentials: dict[str, str]) -> None:
        """Serialize `credentials` to `<config_dir>/credentials.toml` with mode 0600."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        content = format_flat_table("credentials", credentials)
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            _CREDENTIALS_FILE_MODE,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)

    def _warn_fallback(self) -> None:
        """Print the fallback warning to stderr, naming the path -- never a credential value.

        Called unconditionally at the start of every public method (PLAN.md (issue #56)
        §1 D14) -- AC-BI-011 requires the warning on every use, not once per process; and
        AC-BI-015 requires the path, never the credential value, to appear in it.
        """
        print(
            f"⚠️  no OS keyring backend available; using {self._path} instead (mode 0600). "
            "This is less secure than an OS keyring.",
            file=sys.stderr,
        )

    def get_credential(self, context: str) -> str | None:
        """Return the stored credential for `context`, or `None` if none is stored."""
        self._warn_fallback()
        return self._load().get(context)

    def set_credential(self, context: str, credential: str) -> None:
        """Store `credential` for `context`, creating/overwriting `credentials.toml`."""
        self._warn_fallback()
        credentials = self._load()
        credentials[context] = credential
        self._write(credentials)

    def delete_credential(self, context: str) -> None:
        """Remove `context`'s stored credential, if any; a no-op if none exists."""
        self._warn_fallback()
        credentials = self._load()
        if context in credentials:
            del credentials[context]
            self._write(credentials)


_KEYRING_SERVICE_NAME = "ps-cli"


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

    def get_credential(self, context: str) -> str | None:
        """Return `context`'s credential from the keyring, or the file fallback on error.

        Any `keyring.errors.KeyringError` (no backend, locked, init failure) is treated as
        "the backend is unusable" and falls back (D12).
        """
        try:
            return self._keyring_backend.get_password(_KEYRING_SERVICE_NAME, context)
        except keyring.errors.KeyringError:
            return self._fallback.get_credential(context)

    def set_credential(self, context: str, credential: str) -> None:
        """Store `credential` for `context` in the keyring, or the file fallback on error.

        Any `keyring.errors.KeyringError` -- including `PasswordSetError`, this specific
        write failing -- falls back (D12).
        """
        try:
            self._keyring_backend.set_password(_KEYRING_SERVICE_NAME, context, credential)
        except keyring.errors.KeyringError:
            self._fallback.set_credential(context, credential)

    def delete_credential(self, context: str) -> None:
        """Remove `context`'s stored credential from the keyring.

        `PasswordDeleteError` alone means the backend works fine but nothing was stored
        for this context -- a benign no-op, **not** a fallback trigger: no fallback call,
        no warning, no file touched. Any other `KeyringError` (`NoKeyringError`,
        `InitError`, `KeyringLocked`) means the backend is genuinely unusable and falls
        back. This distinction matters because D13's `set-context` mechanism calls
        `delete_credential` unconditionally, including for every brand-new context that
        never had a credential set -- getting it wrong would spuriously fall back (and
        print a misleading warning) on every healthy-keyring `set-context` call. See D12.
        """
        try:
            self._keyring_backend.delete_password(_KEYRING_SERVICE_NAME, context)
        except keyring.errors.PasswordDeleteError:
            return
        except keyring.errors.KeyringError:
            self._fallback.delete_credential(context)


def build_credential_store(config_dir: Path) -> CredentialStore:
    """Build the default `CredentialStore`: the real `keyring` module, file fallback.

    Composes `KeyringCredentialStore` with a `FileCredentialStore(config_dir)` fallback
    and the real `keyring` module as its backend -- the production wiring every CLI
    command uses. See PLAN.md (issue #56) §1 D9.
    """
    return KeyringCredentialStore(fallback=FileCredentialStore(config_dir), keyring_backend=keyring)
