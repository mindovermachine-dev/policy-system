"""Shared pytest fixtures for `ps-cli/tests/`.

See CHANGES.md (issue #56) F2: closes FLAWS.md's blocking finding that, once
`load_config()`'s `config_dir` default becomes `resolve_config_dir()` (real
$HOME/.config/ps-cli/` absent `PS_CLI_CONFIG_DIR`), every pre-existing
`test_config.py` test calling bare `load_config()` would silently start
reading real machine state.

Issue #121, D-121-7: `FileCredentialStore` (and the "no OS keyring -> file
fallback" scenario it used to cover) is gone entirely (AC-BI-008). Before this
issue, "no real OS keyring available" and "test the fallback path" were the
same test scenario -- afterward they are two distinct scenarios with two
distinct correct outcomes, so this module provides two distinct fakes:

- `InMemoryKeyringBackend` -- a portable, always-working in-memory fake, for
  every test that just needs *a* working credential store without depending
  on a real OS keyring backend being present. Promoted from the pattern
  originally hand-rolled in `test_credentials.py`.
- `AlwaysRaisingKeyringBackend` -- unconditionally raises a bare `OSError`
  (deliberately *not* any `keyring.errors.*` subclass), reproducing "the
  failure is a raw `win32ctypes.pywin32.pywintypes.error`, not a
  `keyring.errors.KeyringError`" (TASK.md issue #121) -- reserved for the
  small set of tests that specifically prove AC-BI-006/007/011.

Four fixtures build on these two classes: `keyring_backend`/
`always_raising_keyring_backend` hand back a bare instance for direct
`KeyringCredentialStore(keyring_backend=...)` construction; `portable_keyring`/
`unusable_keyring` additionally monkeypatch the real `keyring` module's three
free functions (`get_password`/`set_password`/`delete_password`) so a test
that exercises `build_credential_store()`'s own zero-argument production
wiring (which always constructs `KeyringCredentialStore(keyring_backend=
keyring)`) gets a portable/always-failing backend instead of depending on
whatever real OS keyring backend (if any) is present on the machine running
the suite. Only the three free functions are patched, never the `keyring`/
`keyring.errors` module symbols themselves -- replacing those breaks
`KeyringCredentialStore.delete_tokens`'s own `except
keyring.errors.PasswordDeleteError` clause (confirmed by this repo's prior
`_force_no_keyring`-style helpers, which used the same restricted mechanism).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
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


class InMemoryKeyringBackend:
    """A hand-written, in-memory `KeyringBackend` fake -- a plain `dict` under the hood.

    No OS/network dependency, portable to any machine or CI runner regardless of what (if
    any) real OS keyring backend is present there (issue #121, D-121-7).
    """

    def __init__(self) -> None:
        """Start with nothing stored."""
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return the stored password for `(service_name, username)`, or `None`."""
        return self._store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Store `password` for `(service_name, username)`."""
        self._store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        """Remove the stored password for `(service_name, username)`, if any."""
        self._store.pop((service_name, username), None)


class AlwaysRaisingKeyringBackend:
    """A `KeyringBackend` fake whose every method unconditionally raises a bare `OSError`.

    Reproduces "the failure is a raw `win32ctypes.pywin32.pywintypes.error`, not a
    `keyring.errors.KeyringError`" (TASK.md, issue #121) -- deliberately *not* any
    `keyring.errors.*` subclass, since the whole point of AC-BI-006/007/011 is that the
    real Windows failure mode is not one of those. Reserved for the small set of tests
    that specifically prove AC-BI-006/007/011 -- every other test that just needs a
    portable working store uses `InMemoryKeyringBackend` instead.
    """

    def get_password(self, service_name: str, username: str) -> str | None:
        """Unconditionally raise a bare `OSError`."""
        del service_name, username
        msg = "simulated non-KeyringError OS keyring backend failure"
        raise OSError(msg)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Unconditionally raise a bare `OSError`."""
        del service_name, username, password
        msg = "simulated non-KeyringError OS keyring backend failure"
        raise OSError(msg)

    def delete_password(self, service_name: str, username: str) -> None:
        """Unconditionally raise a bare `OSError`."""
        del service_name, username
        msg = "simulated non-KeyringError OS keyring backend failure"
        raise OSError(msg)


@pytest.fixture
def keyring_backend() -> InMemoryKeyringBackend:
    """A fresh, portable, working in-memory `KeyringBackend` fake for this test only."""
    return InMemoryKeyringBackend()


@pytest.fixture
def always_raising_keyring_backend() -> AlwaysRaisingKeyringBackend:
    """A fresh, always-raising `KeyringBackend` fake, for AC-BI-006/007/011 proof sites."""
    return AlwaysRaisingKeyringBackend()


@pytest.fixture
def portable_keyring(monkeypatch: pytest.MonkeyPatch) -> InMemoryKeyringBackend:
    """Monkeypatch the real `keyring` module onto a fresh, portable, in-memory fake.

    Lets a test exercise `build_credential_store()`'s real, zero-argument production
    wiring without depending on a real OS keyring backend being present. Returns the
    backend instance itself so a test can also inspect/seed it directly (e.g. via a
    second `KeyringCredentialStore(keyring_backend=<this>)`).
    """
    backend = InMemoryKeyringBackend()
    monkeypatch.setattr("keyring.get_password", backend.get_password)
    monkeypatch.setattr("keyring.set_password", backend.set_password)
    monkeypatch.setattr("keyring.delete_password", backend.delete_password)
    return backend


@pytest.fixture
def unusable_keyring(monkeypatch: pytest.MonkeyPatch) -> AlwaysRaisingKeyringBackend:
    """Monkeypatch the real `keyring` module onto an always-raising fake (bare `OSError`).

    Lets a test exercise `build_credential_store()`'s real, zero-argument production
    wiring against a simulated unusable OS keyring backend (AC-BI-006/007/011), without
    depending on the real OS keyring state of the machine running the suite.
    """
    backend = AlwaysRaisingKeyringBackend()
    monkeypatch.setattr("keyring.get_password", backend.get_password)
    monkeypatch.setattr("keyring.set_password", backend.set_password)
    monkeypatch.setattr("keyring.delete_password", backend.delete_password)
    return backend
