"""Tests for ps_cli.credentials: `CredentialStore`/`KeyringBackend` Protocols and
`FileCredentialStore` (PLAN.md issue #56 §1 D9, D15 -- Slice 15; D14 -- Slice 16).

Slice 16 adds the fallback-warning tests (D14): `FileCredentialStore` prints a warning to
stderr naming the `credentials.toml` path -- never the credential value (AC-BI-015) -- on
every call to `get_credential`/`set_credential`/`delete_credential`, not once per process
(AC-BI-011's literal "every use" wording).

Slices 17-21 add `KeyringCredentialStore` (D9, D11, D12) and `build_credential_store()`
(D9). Slice 18's test design follows CHANGES.md (issue #56) F1 in full, not PLAN.md's
original text -- F1 replaces the original single real-`keyring`-module test with a
portable primary proof (`_AlwaysNoKeyringErrorBackend`, an injected fake -- true on every
platform) and demotes the original real-module test to an explicitly informational,
self-skipping regression check (`skipif keyring.get_keyring().priority > 0`), since a
contributor with a real, working OS keyring backend must not fail the mandatory fast gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import keyring
import keyring.errors
import pytest

from ps_cli.credentials import (
    FileCredentialStore,
    KeyringCredentialStore,
    build_credential_store,
)

if TYPE_CHECKING:
    from pathlib import Path


class _InMemoryKeyringBackend:
    """A hand-written, in-memory `KeyringBackend` fake -- a plain `dict` under the hood.

    No OS/network dependency, portable to any machine or CI runner regardless of what (if
    any) real OS keyring backend is present there. Used to prove `KeyringCredentialStore`'s
    happy path (Slice 17) and per-context isolation (Slice 20) -- see PLAN.md D9's "why
    `keyring_backend` is itself constructor-injected" rationale.
    """

    def __init__(self) -> None:
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


def test_keyring_credential_store_happy_path_never_touches_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set_credential` then `get_credential` round-trips via the fake backend alone.

    The `FileCredentialStore` fallback's warning must never print (`capsys` stderr empty)
    -- proves the happy path bypasses the fallback entirely (PLAN.md Slice 17).
    """
    store = KeyringCredentialStore(
        fallback=FileCredentialStore(tmp_path), keyring_backend=_InMemoryKeyringBackend()
    )

    store.set_credential("dev", "tok")

    assert store.get_credential("dev") == "tok"
    assert capsys.readouterr().err == ""


class _AlwaysNoKeyringErrorBackend:
    """A `KeyringBackend` fake whose every method unconditionally raises `NoKeyringError`.

    The portable, primary AC-BI-011 proof (CHANGES.md issue #56 F1) -- true on every
    platform, not just a devcontainer with a verified-absent OS keyring backend. Reused
    by Slice 19's "no-keyring-error still falls back" comparison case.
    """

    def get_password(self, service_name: str, username: str) -> str | None:
        """Unconditionally raise `NoKeyringError`."""
        del service_name, username
        raise keyring.errors.NoKeyringError("no backend")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Unconditionally raise `NoKeyringError`."""
        del service_name, username, password
        raise keyring.errors.NoKeyringError("no backend")

    def delete_password(self, service_name: str, username: str) -> None:
        """Unconditionally raise `NoKeyringError`."""
        del service_name, username
        raise keyring.errors.NoKeyringError("no backend")


def test_keyring_credential_store_falls_back_to_file_when_backend_raises_no_keyring_error_get(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`get_credential` falls back to the file store when the backend raises `NoKeyringError`.

    Portable primary AC-BI-011 proof (CHANGES.md F1) -- uses an injected fake, not the
    real `keyring` module, so it is true on every platform. `"dev"` is pre-seeded directly
    into the `FileCredentialStore` fallback; `KeyringCredentialStore.get_credential`
    returns that seeded value, no exception escapes, and the fallback warning prints.
    """
    fallback = FileCredentialStore(tmp_path)
    fallback.set_credential("dev", "seeded-tok")
    capsys.readouterr()  # discard the seeding call's own warning
    store = KeyringCredentialStore(
        fallback=fallback, keyring_backend=_AlwaysNoKeyringErrorBackend()
    )

    result = store.get_credential("dev")

    assert result == "seeded-tok"
    assert str(tmp_path / "credentials.toml") in capsys.readouterr().err


def test_keyring_credential_store_falls_back_to_file_when_backend_raises_no_keyring_error_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set_credential` falls back to the file store when the backend raises `NoKeyringError`.

    Portable primary AC-BI-011 proof (CHANGES.md F1). The write must actually land in
    `credentials.toml`, and the fallback warning must print.
    """
    store = KeyringCredentialStore(
        fallback=FileCredentialStore(tmp_path), keyring_backend=_AlwaysNoKeyringErrorBackend()
    )

    store.set_credential("dev", "tok-1")

    assert str(tmp_path / "credentials.toml") in capsys.readouterr().err
    assert FileCredentialStore(tmp_path).get_credential("dev") == "tok-1"


@pytest.mark.skipif(
    keyring.get_keyring().priority > 0,
    reason="informational only: meaningful solely on a machine with no real OS keyring "
    "backend (e.g. this devcontainer). A supported local-dev machine with a "
    "working OS keyring (macOS Keychain / Windows Credential Locker, per "
    "CONTRIBUTING.md's non-devcontainer option) legitimately skips this -- "
    "AC-BI-011 is proven portably by the fake-backend tests above.",
)
def test_real_keyring_module_has_no_backend_informational_devcontainer_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Demoted, self-skipping proof using the real, unmodified `keyring` module.

    Not load-bearing for AC-BI-011 (CHANGES.md F1) -- mirrors `test_cli.py`'s existing
    "real, not mocked" style (`test_run_with_unreachable_real_service_returns_one_
    without_crashing`), but only where it is actually true: this devcontainer's OS
    keyring backend is genuinely absent (PLAN.md §0.7).
    """
    store = KeyringCredentialStore(fallback=FileCredentialStore(tmp_path), keyring_backend=keyring)

    store.set_credential("dev", "tok-1")

    set_captured = capsys.readouterr()
    assert str(tmp_path / "credentials.toml") in set_captured.err
    assert FileCredentialStore(tmp_path).get_credential("dev") == "tok-1"

    result = store.get_credential("dev")

    get_captured = capsys.readouterr()
    assert result == "tok-1"
    assert str(tmp_path / "credentials.toml") in get_captured.err


class _AlwaysPasswordDeleteErrorBackend:
    """A `KeyringBackend` fake whose `delete_password` always raises `PasswordDeleteError`.

    `get_password`/`set_password` never raise -- unused by Slice 19's test but present for
    structural completeness of the `KeyringBackend` protocol.
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


class _UncallableFileCredentialStore(FileCredentialStore):
    """A `FileCredentialStore` stand-in whose methods must never be called.

    Same style as `test_cli.py`'s `_UncallableIngestClient` -- fails the test if reached,
    proving `KeyringCredentialStore.delete_credential` never falls back on a benign
    `PasswordDeleteError` (D12).
    """

    def get_credential(self, context: str) -> str | None:
        """Fail the test if reached."""
        msg = f"get_credential must not be called, got context={context!r}"
        raise AssertionError(msg)

    def set_credential(self, context: str, credential: str) -> None:
        """Fail the test if reached."""
        del credential
        msg = f"set_credential must not be called, got context={context!r}"
        raise AssertionError(msg)

    def delete_credential(self, context: str) -> None:
        """Fail the test if reached."""
        msg = f"delete_credential must not be called, got context={context!r}"
        raise AssertionError(msg)


def test_delete_credential_with_password_delete_error_is_benign_noop_not_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`PasswordDeleteError` is a benign no-op (D12) -- not a fallback trigger.

    The fallback's `delete_credential` must never be called (an uncallable double fails
    the test if reached), and no warning is printed -- the backend works fine, there was
    simply nothing stored for this context.
    """
    store = KeyringCredentialStore(
        fallback=_UncallableFileCredentialStore(tmp_path),
        keyring_backend=_AlwaysPasswordDeleteErrorBackend(),
    )

    store.delete_credential("dev")  # must not raise, must not call the fallback

    assert capsys.readouterr().err == ""


def test_delete_credential_with_no_keyring_error_falls_back_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A genuinely unusable backend (`NoKeyringError`) still falls back and warns (D12).

    The opposite comparison case to the benign-no-op test above -- proves the two
    `keyring.errors` subclasses are not treated identically. `"dev"` is pre-seeded
    directly into the file fallback so the fallback's own `delete_credential` has
    something to actually remove.
    """
    fallback = FileCredentialStore(tmp_path)
    fallback.set_credential("dev", "seeded-tok")
    capsys.readouterr()  # discard the seeding call's own warning
    store = KeyringCredentialStore(
        fallback=fallback, keyring_backend=_AlwaysNoKeyringErrorBackend()
    )

    store.delete_credential("dev")

    assert str(tmp_path / "credentials.toml") in capsys.readouterr().err
    assert FileCredentialStore(tmp_path).get_credential("dev") is None


def test_keyring_credential_store_isolates_credentials_per_context_name(tmp_path: Path) -> None:
    """Two different context names can never collide (AC-BI-013, D11).

    `set_credential("dev", ...)` then `get_credential("prod")` must never return `"dev"`'s
    value -- proves the injected fake's own `(service_name, username)` keying, which
    mirrors exactly what the real `keyring_backend.get_password("ps-cli", context)` call
    does.
    """
    store = KeyringCredentialStore(
        fallback=FileCredentialStore(tmp_path), keyring_backend=_InMemoryKeyringBackend()
    )

    store.set_credential("dev", "dev-tok")

    assert store.get_credential("prod") is None


def test_build_credential_store_wraps_file_fallback_and_real_keyring_module(tmp_path: Path) -> None:
    """`build_credential_store(tmp_path)` wires the production `KeyringCredentialStore`.

    Structural check only (behavior is already covered by Slices 15-20): the returned
    object is a `KeyringCredentialStore` wrapping a `FileCredentialStore(tmp_path)`
    fallback and the real `keyring` module as its backend.
    """
    store = build_credential_store(tmp_path)

    assert isinstance(store, KeyringCredentialStore)
    fallback = store._fallback  # pyright: ignore[reportPrivateUsage]  # Slice 21: structural factory-wiring proof, not behavior
    assert isinstance(fallback, FileCredentialStore)
    assert fallback._config_dir == tmp_path  # pyright: ignore[reportPrivateUsage]  # same
    assert (
        store._keyring_backend  # pyright: ignore[reportPrivateUsage]  # same
        is keyring
    )


def test_file_credential_store_set_then_get_round_trips(tmp_path: Path) -> None:
    """`set_credential` then `get_credential` for the same context returns the same value."""
    store = FileCredentialStore(tmp_path)

    store.set_credential("dev", "tok-1")

    assert store.get_credential("dev") == "tok-1"


def test_file_credential_store_get_returns_none_for_missing_context(tmp_path: Path) -> None:
    """A context with no stored credential resolves to `None`, not an exception."""
    store = FileCredentialStore(tmp_path)

    assert store.get_credential("missing") is None


def test_file_credential_store_writes_with_mode_0600(tmp_path: Path) -> None:
    """`credentials.toml` is written with mode 0600 after any write (PLAN.md D15)."""
    store = FileCredentialStore(tmp_path)

    store.set_credential("dev", "tok-1")

    credentials_path = tmp_path / "credentials.toml"
    assert oct(credentials_path.stat().st_mode)[-3:] == "600"


def test_file_credential_store_delete_removes_entry_then_get_returns_none(
    tmp_path: Path,
) -> None:
    """`delete_credential` removes a stored entry; a subsequent `get_credential` returns `None`."""
    store = FileCredentialStore(tmp_path)
    store.set_credential("dev", "tok-1")

    store.delete_credential("dev")

    assert store.get_credential("dev") is None


def test_file_credential_store_delete_absent_context_is_noop(tmp_path: Path) -> None:
    """Deleting a context that was never stored is a no-op -- no exception raised."""
    store = FileCredentialStore(tmp_path)

    store.delete_credential("never-set")  # must not raise


def test_file_credential_store_warns_on_every_call_naming_path_not_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every call warns on stderr, naming the file path -- the credential value never appears.

    AC-BI-011 (warning half) + AC-BI-015: `get_credential`/`set_credential`/
    `delete_credential` each print a warning naming `credentials.toml`'s path; a
    distinctive, never-otherwise-used secret string passed to `set_credential` must not
    appear anywhere in captured stdout/stderr across any of the three calls below.
    """
    store = FileCredentialStore(tmp_path)
    credentials_path = tmp_path / "credentials.toml"
    secret_value = "super-secret-token-value-should-never-print"

    store.set_credential("dev", secret_value)
    set_captured = capsys.readouterr()
    assert str(credentials_path) in set_captured.err
    assert secret_value not in set_captured.err
    assert secret_value not in set_captured.out

    store.get_credential("dev")
    get_captured = capsys.readouterr()
    assert str(credentials_path) in get_captured.err
    assert secret_value not in get_captured.err
    assert secret_value not in get_captured.out

    store.delete_credential("dev")
    delete_captured = capsys.readouterr()
    assert str(credentials_path) in delete_captured.err
    assert secret_value not in delete_captured.err
    assert secret_value not in delete_captured.out


def test_file_credential_store_warns_every_time_not_only_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning prints on every call, not once per process (AC-BI-011's literal wording)."""
    store = FileCredentialStore(tmp_path)
    credentials_path = tmp_path / "credentials.toml"

    store.get_credential("dev")
    store.get_credential("dev")

    stderr = capsys.readouterr().err
    assert stderr.count(str(credentials_path)) == 2
