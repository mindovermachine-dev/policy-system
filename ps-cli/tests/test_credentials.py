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

from typing import TYPE_CHECKING

import msal_extensions
import pytest

from ps_cli import credentials
from ps_cli.credentials import (
    CredentialStore,
    PersistenceCredentialStore,
    TokenBundle,
    build_credential_store,
)
from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import AlwaysRaisingPersistenceBackend, InMemoryPersistenceBackend

_TOKENS = TokenBundle(refresh_token="refresh-tok", issuer="https://issuer.example")


def test_persistence_credential_store_happy_path_round_trips(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> None:
    """`set_tokens` then `get_tokens` round-trips via the fake backend alone."""
    store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    store.set_tokens("dev", _TOKENS)

    assert store.get_tokens("dev") == _TOKENS


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

    with pytest.raises(PsCliError) as excinfo:
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

    with pytest.raises(PsCliError) as excinfo:
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

    with pytest.raises(PsCliError) as excinfo:
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
