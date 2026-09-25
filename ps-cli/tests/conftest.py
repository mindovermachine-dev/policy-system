"""Shared pytest fixtures for `ps-cli/tests/`.

See CHANGES.md (issue #56) F2: closes FLAWS.md's blocking finding that, once
`load_config()`'s `config_dir` default becomes `resolve_config_dir()` (real
$HOME/.config/ps-cli/` absent `PS_CLI_CONFIG_DIR`), every pre-existing
`test_config.py` test calling bare `load_config()` would silently start
reading real machine state.

Issue #121, D-121-7: `FileCredentialStore` (and the "no OS credential-storage
backend -> file fallback" scenario it used to cover) is gone entirely (AC-BI-008).
Before this issue, "no real OS credential-storage backend available" and "test the
fallback path" were the same test scenario -- afterward they are two distinct
scenarios with two distinct correct outcomes.

Issue #123: the OS-credential-store dependency used through #121 is replaced
outright by `msal-extensions`, whose
`BasePersistence` shape has no `(service_name, username)` key pair -- one
persistence instance is bound to one on-disk location (see
`ps_cli/credentials.py`'s own module docstring, D-123-1/D-123-2). This module
now provides:

- `InMemoryPersistenceBackend` -- a portable, always-working in-memory fake
  bound to one `lock_path`, for every test that just needs *a* working
  persistence backend without depending on a real OS-level secret store being
  present. Replaces the old backend-keyed in-memory fake this module used to
  provide (issue #121, D-121-7).
- `AlwaysRaisingPersistenceBackend` -- unconditionally raises a bare `OSError`
  from `save`/`load` (deliberately *not* any `msal_extensions`-specific type),
  reproducing "the real failure is a raw OS-level exception, not a reshaped
  library type" -- reserved for the small set of tests that specifically prove
  AC-BI-006/007/011. Replaces the old always-raising fake for the previous
  OS-credential-store dependency.

`build_in_memory_persistence`/`build_always_raising_persistence` hand back a
per-context *factory* (`Callable[[str], PersistenceBackend]`, not a single
shared instance) for direct `PersistenceCredentialStore(build_persistence=...)`
construction -- matching `PersistenceCredentialStore`'s own factory-injection
shape (D-123-2), since one persistence instance can no longer serve every
context the way one shared `KeyringBackend` instance could.

`portable_persistence`/`unusable_persistence` additionally monkeypatch
`ps_cli.credentials._build_production_persistence` -- the seam
`build_credential_store()`'s zero-argument production wiring calls internally (the
real msal-extensions-backed factory, see `credentials.py`) -- so a test that
exercises `build_credential_store()`'s own production wiring gets a
portable/always-failing backend instead of depending on real OS state. Same purpose
as before (previously monkeypatching the old OS-credential-store dependency's three
free functions), now hooked at this seam.
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
    or writes the real machine's $HOME/.config/ps-cli/ (PLAN.md D2's default). Individual
    tests may still override PS_CLI_CONFIG_DIR or pass config_dir=... explicitly.
    """
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(tmp_path / "isolated-ps-cli-config"))


class InMemoryPersistenceBackend:
    """A hand-written, in-memory `PersistenceBackend` fake, bound to one `lock_path`.

    No OS/network dependency, portable to any machine or CI runner regardless of what
    (if any) real OS-level secret store is present there (issue #123, replaces the old
    backend-keyed in-memory fake). `lock_path` mirrors a real `BasePersistence`
    instance's own one-instance-one-location shape (D-123-1): `get_location()` returns
    `str(lock_path)` so `PersistenceCredentialStore`'s
    `CrossPlatLock(persistence.get_location() + ".lockfile")` has a real, on-disk path
    to lock, even though this fake's actual stored content lives purely in memory,
    never written to `lock_path` itself.
    """

    def __init__(self, lock_path: Path) -> None:
        """Start with nothing saved; `lock_path` is only used for `get_location()`."""
        self._lock_path = lock_path
        self._content: str | None = None

    def save(self, content: str) -> None:
        """Save `content`, overwriting any existing value."""
        self._content = content

    def load(self) -> str:
        """Return the saved content, or raise `PersistenceNotFound` if never saved."""
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
    the whole point of AC-BI-006/007/011 is that the real Windows/Linux failure mode is
    not one of those. Reserved for the small set of tests that specifically prove
    AC-BI-006/007/011 -- every other test that just needs a portable working store uses
    `InMemoryPersistenceBackend` instead. `get_location()` still returns a real,
    writable path (never raises) so `CrossPlatLock` can actually acquire its lock
    before `save`/`load` raises inside it. Replaces the old always-raising fake for the
    previous OS-credential-store dependency.
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


@pytest.fixture
def build_in_memory_persistence(
    tmp_path: Path,
) -> Callable[[str], InMemoryPersistenceBackend]:
    """A per-context `InMemoryPersistenceBackend` factory, one dict for this test only.

    Replaces the old shared backend fixture for test files that construct
    `PersistenceCredentialStore` directly (e.g. test_mcp_bridge.py) rather than via
    `build_credential_store()` -- gives each distinct `context` string its own backend
    instance and its own on-disk lock path, mirroring D-123-2's
    one-persistence-per-context design (the old shared fake for the previous
    OS-credential-store dependency was one instance keyed internally by
    `(service, username)`; this fixture achieves the same "one fixture instance covers
    every context used in a test" ergonomic the new, differently-shaped way).
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
    """A per-context `AlwaysRaisingPersistenceBackend` factory, for AC-BI-006/007/011
    proof sites -- replaces the old always-raising fixture for the previous
    OS-credential-store dependency.
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
    wiring against a simulated unusable backend (AC-BI-006/007/011), without depending
    on real OS state or ever actually invoking
    `msal_extensions.build_encrypted_persistence`.
    """

    def _build(context: str) -> AlwaysRaisingPersistenceBackend:
        return AlwaysRaisingPersistenceBackend(lock_path=tmp_path / f"{context}.bin")

    monkeypatch.setattr(credentials, "_build_production_persistence", _build)
    return _build
