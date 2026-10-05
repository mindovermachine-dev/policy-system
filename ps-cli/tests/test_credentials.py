"""Tests for ps_cli.credentials: `CredentialStore`/`PersistenceBackend` Protocols and
`PersistenceCredentialStore` (issue #123: msal-extensions-backed credential storage).

Issue #121 drops `FileCredentialStore` entirely (AC-BI-008) -- any backend exception is
caught and raised as an actionable `PsCliError` instead of falling back to a plaintext
file (AC-BI-006/007). `TokenBundle` shrinks to `refresh_token`/`issuer` only
(AC-BI-001) -- `access_token`/`expires_at` are never persisted; the in-memory-only
access token this issue introduces lives in `device_flow.AccessTokenCache` instead
(see `test_device_flow.py`).

Issue #123 replaces the previous OS-credential-store-backed `KeyringCredentialStore`
with `PersistenceCredentialStore`, an msal-extensions-shaped store taking a per-context
persistence *factory* rather than one shared backend instance (D-123-2). There is no
`delete` primitive anywhere in msal-extensions' public API, so `delete_tokens` overwrites
with an empty string, and `get_tokens` treats an empty loaded string the same as
"nothing stored" (D-123-4) -- the old `PasswordDeleteError`-benign-no-op test has no
msal-extensions equivalent and is removed as obsolete, not reshaped.

Portable fakes (`InMemoryPersistenceBackend`/`AlwaysRaisingPersistenceBackend`) live in
`conftest.py` (D-121-7, reshaped for issue #123), shared across every affected test file.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import msal_extensions
import pytest
from msal_extensions.persistence import KeychainPersistence

from ps_cli import credentials
from ps_cli.credentials import (
    CredentialStore,
    PersistenceCredentialStore,
    TokenBundle,
    build_credential_store,
)
from ps_cli.errors import CredentialStoreError, PsCliError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import (
        AlwaysRaisingPersistenceBackend,
        FakeKeychainError,
        InMemoryPersistenceBackend,
    )

_TOKENS = TokenBundle(refresh_token="refresh-tok", issuer="https://issuer.example")


def test_persistence_credential_store_happy_path_round_trips(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """`set_tokens` then `get_tokens` round-trips via the fake backend alone."""
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    store.set_tokens("dev", _TOKENS)

    assert store.get_tokens("dev") == _TOKENS


def test_last_modified_returns_none_when_never_saved_and_moves_after_save(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """Issue #181 AC-BI-004: `last_modified` is the stat-only change signal -- `None`
    before anything was ever saved, a value that moves on every later write (including
    `delete_tokens`' empty-sentinel write), and never reads the stored content.
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)
    assert store.last_modified("dev") is None

    store.set_tokens("dev", _TOKENS)
    after_set = store.last_modified("dev")
    store.delete_tokens("dev")
    after_delete = store.last_modified("dev")

    assert after_set is not None
    assert after_delete is not None
    assert after_delete > after_set
    assert build_in_memory_persistence("dev").loads == 0


def test_persistence_credential_store_persists_only_refresh_token_and_issuer(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-001: only `refresh_token`+`issuer` ever reach the persistence backend's own
    string storage -- verified by inspecting the backend directly (not just through
    `CredentialStore.get_tokens()`), so a bug that leaked a third field into the JSON
    blob would be caught even if `TokenBundle`'s own shape somehow still round-tripped.
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    store.set_tokens("dev", _TOKENS)

    raw = build_in_memory_persistence("dev").load()
    assert '"refresh_token"' in raw
    assert '"issuer"' in raw
    assert '"access_token"' not in raw
    assert '"expires_at"' not in raw


def test_persistence_credential_store_get_returns_none_for_missing_context(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """A context with no stored credential resolves to `None`, not an exception."""
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    assert store.get_tokens("missing") is None


def test_persistence_credential_store_round_trips_with_no_refresh_token(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """A `TokenBundle` with `refresh_token=None` round-trips (AC-BI-002's fail-closed
    "not-logged-in" shape is representable in the store).
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)
    tokens = TokenBundle(refresh_token=None, issuer="https://issuer.example")

    store.set_tokens("dev", tokens)

    assert store.get_tokens("dev") == tokens


def test_persistence_credential_store_round_trips_a_refresh_token_exceeding_the_old_windows_keyring_limit(  # noqa: E501  # test name mirrors PLAN.md Slice 6's own literal test name verbatim
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """AC-BI-005: a synthetic 5000-character `refresh_token` -- comfortably past both the
    ~1280-char figure `TASK.md:9-11` cites and 2560 raw bytes, so the test is robust to
    either interpretation of "the old limit" -- round-trips byte-for-byte through
    `PersistenceCredentialStore`'s JSON-encode -> `save()` -> `load()` -> JSON-decode path.
    Proves the Slice 6 design note's claim directly: our own code imposes no size ceiling
    anywhere in that round trip, unlike the old `WinVaultKeyring`-backed path whose
    ~1280-char ceiling came entirely from `CredWrite`'s own blob-size cap -- a
    Windows-OS-level constraint this store's persistence-agnostic encode/decode path
    never shared and `FilePersistenceWithDataProtection`'s DPAPI-encrypted-file backend
    (unreachable off Windows, see this slice's own design note) does not either.
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)
    oversized_refresh_token = "r" * 5000
    tokens = TokenBundle(refresh_token=oversized_refresh_token, issuer="https://issuer.example")

    store.set_tokens("dev", tokens)

    round_tripped = store.get_tokens("dev")
    assert round_tripped is not None
    assert round_tripped.refresh_token == oversized_refresh_token
    assert len(round_tripped.refresh_token or "") == 5000


def test_persistence_credential_store_isolates_credentials_per_context_name(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """Two different context names can never collide -- each gets its own persistence
    location (D-123-1's isolation guarantee, keyed by filesystem path instead of by
    username string as before D11).

    `set_tokens("dev", ...)` then `get_tokens("prod")` must never return `"dev"`'s
    value -- proves the injected factory's own per-context backend isolation, which
    mirrors exactly what the real production factory's per-context location
    (`resolve_config_dir() / "credentials" / f"{context}.bin"`, a later slice) does.
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    store.set_tokens("dev", _TOKENS)

    assert store.get_tokens("prod") is None


def test_delete_tokens_removes_entry_then_get_returns_none(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """`delete_tokens` removes a stored entry; a subsequent `get_tokens` returns `None`
    (D-123-4: implemented as an empty-string overwrite, observably identical).
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)
    store.set_tokens("dev", _TOKENS)

    store.delete_tokens("dev")

    assert store.get_tokens("dev") is None


def test_delete_tokens_on_a_context_with_nothing_stored_is_a_benign_noop(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """`delete_tokens` on a context nothing was ever saved for must not raise, and a
    subsequent `get_tokens` must still return `None` (PLAN.md Slice 3 test 3;
    CHANGES.md row 1 replaces the old `PasswordDeleteError`-benign-no-op test, which
    has no msal-extensions equivalent, with this one -- proves the same "delete of
    nothing is safe" property through D-123-4's unconditional empty-string overwrite
    instead of a caught delete-specific exception type).
    """
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    store.delete_tokens("dev")

    assert store.get_tokens("dev") is None


def test_get_tokens_with_non_persistence_error_exception_raises_actionable_ps_cli_error(
    build_always_raising_persistence: Callable[[str], AlwaysRaisingPersistenceBackend],
) -> None:
    """AC-BI-006/011: a bare `OSError` (not `msal_extensions.persistence.
    PersistenceNotFound`) from `load` is caught and raised as `PsCliError`, naming the
    context and the exception type -- never falling back to a file, never leaking a
    token value (AC-BI-007).
    """
    store = PersistenceCredentialStore(build_persistence=build_always_raising_persistence)

    with pytest.raises(CredentialStoreError) as excinfo:
        store.get_tokens("dev")

    assert "dev" in excinfo.value.msg
    assert "OSError" in (excinfo.value.hint or "")


def test_set_tokens_with_non_persistence_error_exception_raises_actionable_ps_cli_error(
    build_always_raising_persistence: Callable[[str], AlwaysRaisingPersistenceBackend],
) -> None:
    """AC-BI-006/007/008/011: a bare `OSError` from `save` raises `PsCliError`; the
    raised message/hint never contain the token value that was being stored.
    """
    store = PersistenceCredentialStore(build_persistence=build_always_raising_persistence)
    secret_value = "super-secret-refresh-token-should-never-print"
    tokens = TokenBundle(refresh_token=secret_value, issuer="https://issuer.example")

    with pytest.raises(CredentialStoreError) as excinfo:
        store.set_tokens("dev", tokens)

    assert "dev" in excinfo.value.msg
    assert secret_value not in excinfo.value.msg
    assert secret_value not in (excinfo.value.hint or "")
    assert "OSError" in (excinfo.value.hint or "")


def test_delete_tokens_with_non_persistence_error_exception_raises_actionable_ps_cli_error(
    build_always_raising_persistence: Callable[[str], AlwaysRaisingPersistenceBackend],
) -> None:
    """AC-BI-006/011: a bare `OSError` from `save` (delete's empty-string overwrite)
    raises `PsCliError` -- `delete_tokens` has no benign-no-op special case any more
    (D-123-4), so every backend exception here is genuine.
    """
    store = PersistenceCredentialStore(build_persistence=build_always_raising_persistence)

    with pytest.raises(CredentialStoreError) as excinfo:
        store.delete_tokens("dev")

    assert "dev" in excinfo.value.msg
    assert "OSError" in (excinfo.value.hint or "")


def test_file_persistence_satisfies_the_persistence_backend_protocol(tmp_path: Path) -> None:
    """CHANGES.md row 3: a real `msal_extensions.FilePersistence` -- never
    `build_encrypted_persistence` (D-123-5's "never plaintext" rule is about what
    production code constructs, not about this base-class shape check) -- structurally
    satisfies `PersistenceBackend`'s `save`/`load`/`get_location` shape, proven with a
    real, on-disk, unencrypted round trip. CI-safe on every platform: `FilePersistence`
    has no OS-native dependency, unlike `build_encrypted_persistence`'s platform-specific
    subclasses. Catches a future `msal-extensions` release changing `BasePersistence`'s
    shape, which a `cast()`-only structural check would not.
    """
    persistence: credentials.PersistenceBackend = msal_extensions.FilePersistence(
        tmp_path / "smoke.bin"
    )

    persistence.save("x")

    assert persistence.load() == "x"
    assert persistence.get_location() == str(tmp_path / "smoke.bin")


def test_build_production_persistence_with_missing_pygobject_raises_actionable_ps_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009/D-123-3: when PyGObject itself is missing, msal-extensions' own
    `libsecret.py` raises a bare `ImportError` (not a reshaped library type) whose
    message names the exact `apt install` command. `_build_production_persistence`
    must catch it and re-raise an actionable `PsCliError` -- verified by monkeypatching
    only `msal_extensions.build_encrypted_persistence` (the specific function, never the
    whole module) to raise the library's own exact message text, copied verbatim from
    the installed msal-extensions 1.3.1 source (`libsecret.py:20-26`) rather than
    paraphrased, so this test would fail if a future `msal-extensions` release changed
    that wording and this repo's `PsCliError.hint` silently stopped matching it.
    """
    real_pygobject_missing_message = (
        "Unable to import module 'gi'\n"
        "Runtime dependency of PyGObject is missing.\n"
        "Depends on your Linux distro, you could install it system-wide by something "
        "like:\n"
        "    sudo apt install python3-gi python3-gi-cairo gir1.2-secret-1\n"
        "If necessary, please refer to PyGObject's doc:\n"
        "https://pygobject.readthedocs.io/en/latest/getting_started.html\n"
    )

    def _raise_import_error(location: str) -> credentials.PersistenceBackend:
        raise ImportError(real_pygobject_missing_message)

    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", _raise_import_error)

    with pytest.raises(PsCliError) as excinfo:
        credentials._build_production_persistence(  # pyright: ignore[reportPrivateUsage]  # exercising the exact catch site, not the public seam
            "dev"
        )

    assert "gir1.2-secret-1" in (excinfo.value.hint or "")
    assert "missing" in excinfo.value.msg
    assert "system package" in excinfo.value.msg


def test_build_production_persistence_with_version_mismatch_raises_actionable_ps_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009/D-123-3: the second exception type the same catch site must handle --
    when PyGObject is present but `gi.require_version("Secret", "1")` can't satisfy the
    requested version (or the subsequent `from gi.repository import Secret` fails),
    msal-extensions' `libsecret.py` re-raises `type(ex)(...)` over a `(ValueError,
    ImportError)` catch (`libsecret.py:28-37`) -- `gi.require_version` itself raises
    `ValueError` for an unresolvable namespace/version, so `ValueError` is the case this
    test exercises; the message text is copied verbatim from the installed
    msal-extensions 1.3.1 source, not paraphrased. Proves AC-BI-009 catches both
    exception types, not just `ImportError`.
    """
    real_version_mismatch_message = (
        'Require a package "gir1.2-secret-1" which could be installed by:\n'
        "        sudo apt install gir1.2-secret-1\n"
        "        "
    )

    def _raise_value_error(location: str) -> credentials.PersistenceBackend:
        raise ValueError(real_version_mismatch_message)

    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", _raise_value_error)

    with pytest.raises(PsCliError) as excinfo:
        credentials._build_production_persistence(  # pyright: ignore[reportPrivateUsage]  # exercising the exact catch site, not the public seam
            "dev"
        )

    assert "gir1.2-secret-1" in (excinfo.value.hint or "")
    assert "missing" in excinfo.value.msg
    assert "system package" in excinfo.value.msg


def test_build_credential_store_wires_the_production_persistence_factory() -> None:
    """`build_credential_store()` wires the production `PersistenceCredentialStore`.

    Zero-argument (AC-BI-008: no `config_dir`, no `FileCredentialStore` anywhere in
    the get/set/delete path) -- structural identity check only, never actually calling
    `_build_production_persistence` (and so never invoking
    `msal_extensions.build_encrypted_persistence` for real) -- no test in this suite
    touches a real OS keychain/DPAPI/libsecret store. Behavior of
    `PersistenceCredentialStore`'s own methods is already covered above against fakes;
    this only proves `build_credential_store()` wires the real production seam,
    `_build_production_persistence` (`credentials.py`'s own module docstring).
    """
    store: CredentialStore = build_credential_store()

    assert isinstance(store, PersistenceCredentialStore)
    assert (
        store._build_persistence  # pyright: ignore[reportPrivateUsage]  # structural factory-wiring proof, not behavior
        is credentials._build_production_persistence  # pyright: ignore[reportPrivateUsage]  # same reason
    )


class _FlakyReadPersistence:
    """Raises each queued exception from `load` once, then serves `content` (issue #180)."""

    def __init__(self, lock_path: Path, failures: list[Exception], content: str) -> None:
        self._lock_path = lock_path
        self._failures = failures
        self._content = content
        self.loads = 0

    def save(self, content: str) -> None:
        self._content = content

    def time_last_modified(self) -> float:
        return 1.0

    def load(self) -> str:
        self.loads += 1
        if self._failures:
            raise self._failures.pop(0)
        return self._content

    def get_location(self) -> str:
        return str(self._lock_path)


def test_get_tokens_retries_a_transient_keychain_error_and_returns_the_rewritten_value(
    tmp_path: Path, fake_keychain_error: type[FakeKeychainError]
) -> None:
    """AC-BI-001/003: a read racing another process's rewrite (`-67701`) is retried."""
    encoded = credentials._encode_token_bundle(_TOKENS)  # pyright: ignore[reportPrivateUsage]
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", [fake_keychain_error(-67701)], encoded)
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    assert store.get_tokens("dev") == _TOKENS
    assert backend.loads == 2


def test_get_tokens_retries_a_not_found_read_caught_mid_rewrite(tmp_path: Path) -> None:
    """AC-BI-001/003: `PersistenceNotFound` while the item is mid-rewrite is retried."""
    encoded = credentials._encode_token_bundle(_TOKENS)  # pyright: ignore[reportPrivateUsage]
    not_found = msal_extensions.persistence.PersistenceNotFound(message="x", location="y")
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", [not_found], encoded)
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    assert store.get_tokens("dev") == _TOKENS


def test_get_tokens_after_logout_returns_none_not_keychain_error(
    tmp_path: Path, fake_keychain_error: type[FakeKeychainError]
) -> None:
    """AC-BI-002: a removed credential (empty sentinel) reads as 'nothing stored'."""
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", [fake_keychain_error(-67701)], "")
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    assert store.get_tokens("dev") is None


def test_get_tokens_persistent_failure_still_fails_closed_with_status_and_no_token(
    tmp_path: Path, fake_keychain_error: type[FakeKeychainError]
) -> None:
    """AC-BI-004/005: a genuinely unavailable backend raises after bounded retries; the
    hint carries the numeric status and never a token value.
    """
    failures: list[Exception] = [fake_keychain_error(-25308) for _ in range(10)]
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", failures, "refresh-tok-secret")
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    with pytest.raises(CredentialStoreError) as excinfo:
        store.get_tokens("dev")

    assert backend.loads == credentials._READ_ATTEMPTS  # pyright: ignore[reportPrivateUsage]
    assert "-25308" in (excinfo.value.hint or "")
    assert "refresh-tok-secret" not in (excinfo.value.hint or "") + excinfo.value.msg


def test_persistent_minus_67701_hint_says_auth_login_not_unlock(
    tmp_path: Path, fake_keychain_error: type[FakeKeychainError]
) -> None:
    """Issue #181 AC-BI-008: -67701 (errSecInvalidRecord) is not a locked keychain."""
    failures: list[Exception] = [fake_keychain_error(-67701) for _ in range(10)]
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", failures, "refresh-tok-secret")
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    with pytest.raises(CredentialStoreError) as excinfo:
        store.get_tokens("dev")

    hint = excinfo.value.hint or ""
    assert excinfo.value.status == -67701
    assert "ps-cli auth login" in hint
    assert "unlocked" not in hint
    assert "(status -67701)" in hint


def test_other_statuses_keep_the_unlock_hint(
    tmp_path: Path, fake_keychain_error: type[FakeKeychainError]
) -> None:
    """Issue #181 AC-BI-008: every other status keeps the existing hint byte for byte."""
    failures: list[Exception] = [fake_keychain_error(-25308) for _ in range(10)]
    backend = _FlakyReadPersistence(tmp_path / "dev.bin", failures, "")
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )

    with pytest.raises(CredentialStoreError) as excinfo:
        store.get_tokens("dev")

    assert excinfo.value.status == -25308
    assert excinfo.value.hint == (
        "the credential-storage backend raised FakeKeychainError (status -25308); "
        "check that it is available and unlocked"
    )


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="reproduces the macOS Keychain rewrite race")
def test_live_keychain_reader_survives_concurrent_rewrites(tmp_path: Path) -> None:
    """Issue #180 reproduction (AC-BI-001/003): a long-lived reader against the real
    macOS Keychain, while another process rewrites the same entry, never raises.

    Uses a throwaway service/account, deleted afterwards -- never a real credential.
    """
    service, account = "ps-cli-issue180-test", "live-race"
    location = str(tmp_path / "race.bin")
    writer = (
        "import time\n"
        "from msal_extensions.persistence import KeychainPersistence\n"
        f"p = KeychainPersistence({location!r}, service_name={service!r}, "
        f"account_name={account!r})\n"
        "for i in range(60):\n"
        '    p.save(\'{"refresh_token": "r%d", "issuer": "i"}\' % i); time.sleep(0.05)\n'
    )
    persistence = KeychainPersistence(location, service_name=service, account_name=account)
    persistence.save(  # pyright: ignore[reportUnknownMemberType]  # msal-extensions: no py.typed
        '{"refresh_token": "r", "issuer": "i"}'
    )
    store = PersistenceCredentialStore(build_persistence=lambda _c: persistence)
    proc = subprocess.Popen([sys.executable, "-c", writer])  # noqa: S603  # fixed args, test-only
    try:
        failures = 0
        while proc.poll() is None:
            try:
                store.get_tokens("race")
            except CredentialStoreError:
                failures += 1
    finally:
        proc.wait()
        subprocess.run(  # noqa: S603  # fixed args
            ["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account],
            check=False,
            capture_output=True,
        )
    assert failures == 0


class _ContentEchoingPersistence:
    """Every call raises an exception whose text embeds the stored content (issue #181).

    Models a backend whose own error message carries the secret it just handled, so the
    tests below prove `PersistenceCredentialStore` never relays that text.
    """

    def __init__(self, lock_path: Path, content: str, exit_status: int) -> None:
        self._lock_path = lock_path
        self._content = content
        self._exit_status = exit_status

    def _failure(self) -> OSError:
        error = OSError(f"backend failed handling {self._content}")
        error.exit_status = self._exit_status  # pyright: ignore[reportAttributeAccessIssue]  # mimics KeychainError
        return error

    def save(self, content: str) -> None:
        del content
        raise self._failure()

    def time_last_modified(self) -> float:
        raise self._failure()

    def load(self) -> str:
        raise self._failure()

    def get_location(self) -> str:
        return str(self._lock_path)


@pytest.mark.parametrize("operation", ["get", "get_observed", "set", "delete", "last_modified"])
def test_credential_store_error_str_and_repr_never_contain_stored_content(
    tmp_path: Path, operation: str
) -> None:
    """Issue #181 AC-BI-010: a backend error echoing the secret is never relayed."""
    secret = "RT-SENTINEL-9f3a"
    backend = _ContentEchoingPersistence(tmp_path / "dev.bin", secret, -67701)
    store = PersistenceCredentialStore(
        build_persistence=lambda _c: backend, read_retry_delay_seconds=0
    )
    operations: dict[str, Callable[[], object]] = {
        "get": lambda: store.get_tokens("dev"),
        "get_observed": lambda: store.get_tokens_observed("dev"),
        "set": lambda: store.set_tokens("dev", TokenBundle(refresh_token=secret, issuer="i")),
        "delete": lambda: store.delete_tokens("dev"),
        "last_modified": lambda: store.last_modified("dev"),
    }

    with pytest.raises(CredentialStoreError) as excinfo:
        operations[operation]()

    error = excinfo.value
    surfaced = [str(error), repr(error), error.msg, error.hint or "", repr(error.args)]
    assert not any(secret in text for text in surfaced)
    assert error.status == -67701
