"""Shared pytest fixtures for `ps-cli/tests/`.

Every test gets an isolated, empty config dir by default, so a test calling bare
`load_config()` never reads the real machine's `$HOME/.config/ps-cli/`.

Credential storage is `msal-extensions` based: its `BasePersistence` shape has no
`(service_name, username)` key pair -- one persistence instance is bound to one
on-disk location (see `ps_cli/credentials.py`'s module docstring). There is no file
fallback store, so "no real OS credential-storage backend available" is a distinct
scenario from any fallback path. This module provides:

- `InMemoryPersistenceBackend` -- a portable, always-working in-memory fake
  bound to one `lock_path`, for every test that just needs *a* working
  persistence backend without depending on a real OS-level secret store being
  present.
- `AlwaysRaisingPersistenceBackend` -- unconditionally raises a bare `OSError`
  from `save`/`load` (deliberately *not* any `msal_extensions`-specific type),
  reproducing "the real failure is a raw OS-level exception, not a reshaped
  library type" -- reserved for the small set of tests that specifically prove
  the unusable-backend behaviour.

`build_in_memory_persistence`/`build_always_raising_persistence` hand back a
per-context *factory* (`Callable[[str], PersistenceBackend]`, not a single
shared instance) for direct `PersistenceCredentialStore(build_persistence=...)`
construction -- matching `PersistenceCredentialStore`'s own factory-injection
shape, since one persistence instance cannot serve more than one context.

`portable_persistence`/`unusable_persistence` additionally monkeypatch
`ps_cli.credentials._build_production_persistence` -- the seam
`build_credential_store()`'s zero-argument production wiring calls internally (the
real msal-extensions-backed factory, see `credentials.py`) -- so a test that
exercises `build_credential_store()`'s own production wiring gets a
portable/always-failing backend instead of depending on real OS state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msal_extensions.persistence
import pytest

from ps_cli import credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_ps_cli_config_dir(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ps-cli test gets a fresh, empty config dir by default so no test ever reads
    or writes the real machine's $HOME/.config/ps-cli/ (the default). Individual
    tests may still override PS_CLI_CONFIG_DIR or pass config_dir=... explicitly.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path / "isolated-ps-cli-config"))


class InMemoryPersistenceBackend:
    """A hand-written, in-memory `PersistenceBackend` fake, bound to one `lock_path`.

    No OS/network dependency, portable to any machine or CI runner regardless of what
    (if any) real OS-level secret store is present there. `lock_path` mirrors a real
    `BasePersistence` instance's own one-instance-one-location shape: `get_location()` returns
    `str(lock_path)` so `PersistenceCredentialStore`'s
    `CrossPlatLock(persistence.get_location() + ".lockfile")` has a real, on-disk path
    to lock, even though this fake's actual stored content lives purely in memory,
    never written to `lock_path` itself.
    """

    def __init__(self, lock_path: Path) -> None:
        """Start with nothing saved; `lock_path` is only used for `get_location()`."""
        self._lock_path = lock_path
        self._content: str | None = None
        self.loads = 0
        self.fail_load_with: Exception | None = None
        self.fail_save_with: Exception | None = None

    def save(self, content: str) -> None:
        """Save `content`, overwriting any existing value.

        Raises `fail_save_with` instead (nothing stored) while armed.
        """
        if self.fail_save_with is not None:
            raise self.fail_save_with
        self._content = content

    def load(self) -> str:
        """Return the saved content, or raise `PersistenceNotFound` if never saved.

        Raises `fail_load_with` instead, on every call, while a test has it armed.
        """
        self.loads += 1
        if self.fail_load_with is not None:
            raise self.fail_load_with
        if self._content is None:
            raise msal_extensions.persistence.PersistenceNotFound(
                message="nothing saved yet", location=str(self._lock_path)
            )
        return self._content

    def get_location(self) -> str:
        """Return this fake's bound lock path, as a string."""
        return str(self._lock_path)


class AlwaysRaisingPersistenceBackend:
    """A `PersistenceBackend` fake whose `save`/`load` unconditionally raise `OSError`.

    Reproduces "the real failure is a raw OS-level exception, not a reshaped
    `msal_extensions`-specific type" -- deliberately *not*
    `msal_extensions.persistence.PersistenceNotFound` or any other library type, since
    the whole point of those tests is that the real Windows/Linux failure mode is
    not one of those. Reserved for the small set of tests that specifically prove
    the unusable-backend behaviour -- every other test that just needs a portable working store uses
    `InMemoryPersistenceBackend` instead. `get_location()` still returns a real,
    writable path (never raises) so `CrossPlatLock` can actually acquire its lock
    before `save`/`load` raises inside it.
    """

    def __init__(self, lock_path: Path) -> None:
        """Store `lock_path` for `get_location()`; every other method always raises."""
        self._lock_path = lock_path

    def save(self, content: str) -> None:
        """Unconditionally raise a bare `OSError`."""
        del content
        msg = "simulated non-msal-extensions persistence backend failure"
        raise OSError(msg)

    def load(self) -> str:
        """Unconditionally raise a bare `OSError`."""
        msg = "simulated non-msal-extensions persistence backend failure"
        raise OSError(msg)

    def get_location(self) -> str:
        """Return this fake's bound lock path, as a string -- never raises."""
        return str(self._lock_path)


class FakeKeychainError(OSError):
    """Mimics `msal_extensions.osx.KeychainError`, which carries `exit_status`."""

    def __init__(self, exit_status: int) -> None:
        super().__init__()
        self.exit_status = exit_status


@pytest.fixture
def fake_keychain_error() -> type[FakeKeychainError]:
    """The `FakeKeychainError` class itself (conftest classes are fixture-injected only)."""
    return FakeKeychainError


@pytest.fixture
def build_in_memory_persistence(
    tmp_path: Path,
) -> Callable[[str], InMemoryPersistenceBackend]:
    """A per-context `InMemoryPersistenceBackend` factory, one dict for this test only.

    Replaces the old shared backend fixture for test files that construct
    `PersistenceCredentialStore` directly (e.g. test_credentials.py) rather than via
    For test files that construct `PersistenceCredentialStore` directly (e.g.
    test_credentials.py) rather than via `build_credential_store()`. Gives each
    distinct `context` string its own backend instance and its own on-disk lock
    path, mirroring the one-persistence-per-context design, so one fixture instance
    covers every context used in a test.
    """
    backends: dict[str, InMemoryPersistenceBackend] = {}

    def _build(context: str) -> InMemoryPersistenceBackend:
        return backends.setdefault(
            context, InMemoryPersistenceBackend(lock_path=tmp_path / f"{context}.bin")
        )

    return _build


@pytest.fixture
def build_always_raising_persistence(
    tmp_path: Path,
) -> Callable[[str], AlwaysRaisingPersistenceBackend]:
    """A per-context `AlwaysRaisingPersistenceBackend` factory, for the tests that
    prove behaviour against an unusable credential backend.
    """

    def _build(context: str) -> AlwaysRaisingPersistenceBackend:
        return AlwaysRaisingPersistenceBackend(lock_path=tmp_path / f"{context}.bin")

    return _build


@pytest.fixture
def portable_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], InMemoryPersistenceBackend]:
    """Monkeypatch `build_credential_store()`'s production wiring
    (`ps_cli.credentials._build_production_persistence`) onto a portable, in-memory
    factory, and return that same factory.

    Lets a test exercise `build_credential_store()`'s real, zero-argument production
    wiring without depending on a real OS-level secret store being present, or ever
    actually invoking `msal_extensions.build_encrypted_persistence` (no automated test
    in this suite touches a real OS keychain/DPAPI/libsecret store). Returns the
    factory itself so a test can also construct a second
    `PersistenceCredentialStore(build_persistence=<this>)` against the identical
    backend `build_credential_store()`'s own wiring now uses.
    """
    backends: dict[str, InMemoryPersistenceBackend] = {}

    def _build(context: str) -> InMemoryPersistenceBackend:
        return backends.setdefault(
            context, InMemoryPersistenceBackend(lock_path=tmp_path / f"{context}.bin")
        )

    monkeypatch.setattr(credentials, "_build_production_persistence", _build)
    return _build


@pytest.fixture
def unusable_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], AlwaysRaisingPersistenceBackend]:
    """Monkeypatch `build_credential_store()`'s production wiring
    (`ps_cli.credentials._build_production_persistence`) onto an always-raising
    factory (bare `OSError`), and return that same factory.

    Lets a test exercise `build_credential_store()`'s real, zero-argument production
    wiring against a simulated unusable backend (unusable backend), without depending
    on real OS state or ever actually invoking
    `msal_extensions.build_encrypted_persistence`.
    """

    def _build(context: str) -> AlwaysRaisingPersistenceBackend:
        return AlwaysRaisingPersistenceBackend(lock_path=tmp_path / f"{context}.bin")

    monkeypatch.setattr(credentials, "_build_production_persistence", _build)
    return _build
