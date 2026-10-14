"""ps-cli credential storage: per-context refresh tokens in an encrypted OS-backed store.

`CredentialStore` is the abstraction every caller depends on. Its only implementation,
`PersistenceCredentialStore`, keeps one `TokenBundle` (refresh token and issuer) per
context in an encrypted file managed by `msal-extensions`, protected by the operating
system's secret store (Windows DPAPI, macOS Keychain, Linux Secret Service). Access
tokens are never persisted; they live in memory in `device_flow.AccessTokenCache`
for one CLI invocation.

Design points:

- There is no plaintext fallback. Any backend failure is raised as an actionable
  `CredentialStoreError` rather than silently writing tokens to a file.
- `PersistenceCredentialStore` takes a per-context persistence *factory*
  (`Callable[[str], PersistenceBackend]`) because a persistence object is bound to one
  on-disk location. The factory is called once per `get_tokens`/`set_tokens`/
  `delete_tokens` call.
- Every read and write holds a `msal_extensions.CrossPlatLock`, so two concurrent
  `ps-cli` invocations against the same context cannot lose an update.
- `msal-extensions` has no delete primitive, so `delete_tokens` overwrites the entry
  with an empty string and `get_tokens` treats an empty or missing entry as "nothing
  stored".
- `build_credential_store()` takes no arguments; it wires `_build_production_persistence`,
  which tests replace with `monkeypatch`.
- `CredentialStoreError` carries the backend's integer `status`, and the hint is chosen
  from it (`_STATUS_HINTS`: -67701 `errSecInvalidRecord` points at `ps-cli auth login`;
  every other status keeps the "available and unlocked" text). Errors surface only the
  exception type name and that integer, never the backend's message, which could embed
  stored content.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import msal_extensions
import msal_extensions.persistence

from ps_cli.errors import CredentialStoreError, PsCliError
from ps_cli.targets import resolve_config_dir

if TYPE_CHECKING:
    from collections.abc import Callable

# A read that lands while another process rewrites the same entry can fail
# transiently (macOS Keychain `-67701` "invalid record", or a `PersistenceNotFound` for
# an item caught mid-rewrite). A few short re-reads let a running process pick up the
# rewritten value; a genuinely absent or unavailable store still fails after the last.
_READ_ATTEMPTS = 4
_READ_RETRY_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class TokenBundle:
    """The only two fields `CredentialStore` persists per context.

    The access token and its expiry are deliberately not part of this shape; they live
    in memory in `device_flow.AccessTokenCache`. Immutable.

    Attributes:
        refresh_token: The OIDC refresh token, or `None` when the IdP issued none (the
            context can then not be refreshed and must log in again).
        issuer: The issuer URL that minted the token.
    """

    refresh_token: str | None
    issuer: str


class CredentialStore(Protocol):
    """A per-context token-bundle store: get, set and delete, keyed by context name.

    A `Protocol` so callers depend on the interface and tests can substitute an
    in-memory fake. Implementations never raise for "nothing stored"; they signal it
    through the return value.
    """

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored.

        Args:
            context: The context name the bundle is keyed by.

        Returns:
            The stored bundle, or `None` when nothing is stored for `context`.

        Raises:
            CredentialStoreError: The backing store could not be read.

        Side Effects:
            Reads the backing store; implementations may briefly retry transient
            failures.
        """
        ...

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing value.

        Args:
            context: The context name to key the bundle by.
            tokens: The bundle to persist.

        Returns:
            None.

        Raises:
            CredentialStoreError: The backing store could not be written.

        Side Effects:
            Writes the backing store.
        """
        ...

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op if none exists.

        Args:
            context: The context name whose bundle is removed.

        Returns:
            None.

        Raises:
            CredentialStoreError: The backing store could not be written.

        Side Effects:
            Modifies the backing store.
        """
        ...


class PersistenceBackend(Protocol):
    """Structural interface matching msal-extensions' own `BasePersistence` shape.

    Lets `PersistenceCredentialStore` take its per-context persistence object as an
    injected value (through a factory) rather than importing `msal_extensions` at every
    call site. A `Protocol` cannot express "raises X", so callers catch
    `msal_extensions.persistence.PersistenceNotFound` directly rather than a
    module-local replacement type.
    """

    def save(self, content: str) -> None:
        """Save `content` into this persistence, overwriting any existing content.

        Args:
            content: The opaque string to store; the empty string is the "nothing
                stored" sentinel.

        Returns:
            None.

        Raises:
            Exception: Any backend failure (for example an OS-level error); callers
                treat every exception as a store failure.

        Side Effects:
            Writes the persistence's on-disk location.
        """
        ...

    def load(self) -> str:
        """Return this persistence's stored content.

        Returns:
            The string last passed to `save`.

        Raises:
            msal_extensions.persistence.PersistenceNotFound: `save()` was never called
                for this location.
            Exception: Any other backend failure.

        Side Effects:
            Reads the persistence's on-disk location.
        """
        ...

    def get_location(self) -> str:
        """Return the on-disk path this persistence instance stores its data in.

        Returns:
            The path as a string; the store appends `.lockfile` to it to build its
            cross-process lock path.

        Raises:
            Nothing.

        Side Effects:
            None.
        """
        ...


def _encode_token_bundle(tokens: TokenBundle) -> str:
    """JSON-encode `tokens` into the single opaque string a persistence backend accepts.

    The only place a `TokenBundle` is encoded; callers of the public store methods only
    ever see structured `TokenBundle` values.
    """
    return json.dumps({"refresh_token": tokens.refresh_token, "issuer": tokens.issuer})


def _decode_token_bundle(raw: str) -> TokenBundle:
    """Decode a `TokenBundle` from the opaque string `_encode_token_bundle()` produced.

    The shape is trusted because `_encode_token_bundle()` is the only producer of the
    stored string; a hand-edited entry raises `json.JSONDecodeError` or `KeyError`.
    """
    decoded = cast("dict[str, object]", json.loads(raw))
    return TokenBundle(
        refresh_token=cast("str | None", decoded["refresh_token"]),
        issuer=cast("str", decoded["issuer"]),
    )


ERR_SEC_INVALID_RECORD = -67701  # macOS errSecInvalidRecord: not a locked keychain

_DEFAULT_STORE_HINT = "check that it is available and unlocked"

# Per-status remediation text; any status not listed (or none) uses `_DEFAULT_STORE_HINT`.
_STATUS_HINTS: dict[int, str] = {
    ERR_SEC_INVALID_RECORD: "the stored credential record is invalid; run `ps-cli auth login` "
    "to store a new one",
}


def _store_error(context: str, exc: Exception) -> CredentialStoreError:
    """Build the actionable store error; the backend's numeric status (if any) aids diagnosis.

    Only the exception type and an integer `exit_status` are surfaced, never a message
    that could embed stored content.
    """
    raw_status = getattr(exc, "exit_status", None)
    status = raw_status if isinstance(raw_status, int) else None
    detail = type(exc).__name__
    if status is not None:
        detail += f" (status {status})"
    remediation = (
        _STATUS_HINTS.get(status, _DEFAULT_STORE_HINT)
        if status is not None
        else _DEFAULT_STORE_HINT
    )
    return CredentialStoreError(
        msg=f"could not access the credential store for context '{context}'",
        hint=f"the credential-storage backend raised {detail}; {remediation}",
        status=status,
    )


class PersistenceCredentialStore:
    """`CredentialStore` backed by `msal-extensions` encrypted per-context persistence.

    `build_persistence` is injected (the real factory via `build_credential_store()`,
    a hand-written in-memory fake in tests) rather than imported, the same
    constructor-injection seam `PsServiceClient`'s `transport` uses. It is a factory
    called once per method invocation, because one persistence object is bound to
    exactly one on-disk location and so cannot be shared across contexts.

    There is no delete primitive in `msal-extensions`: `delete_tokens` overwrites with
    an empty string and `get_tokens` treats an empty loaded string as "nothing stored".
    Any backend failure is raised as `CredentialStoreError`; nothing is ever written
    in plaintext as a fallback.
    """

    def __init__(
        self,
        *,
        build_persistence: Callable[[str], PersistenceBackend],
        read_retry_delay_seconds: float = _READ_RETRY_DELAY_SECONDS,
    ) -> None:
        """Create a store that obtains each context's persistence from `build_persistence`.

        Args:
            build_persistence: Factory mapping a context name to the persistence object
                bound to that context's on-disk location.
            read_retry_delay_seconds: Pause in seconds between `get_tokens` re-reads;
                tests pass `0` instead of patching the module constant. Non-negative.
        """
        self._build_persistence = build_persistence
        self._read_retry_delay_seconds = read_retry_delay_seconds

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return `context`'s `TokenBundle`, or `None` if none is stored.

        A read that lands while another process rewrites the entry can fail
        transiently, so a failing read is retried a few times with a short pause before
        it is treated as final. "Not found" and an empty entry (the delete sentinel)
        both mean nothing is stored. The backend's opaque string is JSON-decoded into
        a `TokenBundle`; callers never see the raw string.

        Args:
            context: The context name the bundle is keyed by.

        Returns:
            The stored bundle, or `None` when nothing is stored for `context`.

        Raises:
            CredentialStoreError: Every read attempt failed with an error other than
                "not found"; the message names the exception type and numeric status
                only, never backend text.
            json.JSONDecodeError: The stored entry is not valid JSON.
            KeyError: The stored entry lacks `refresh_token` or `issuer`.

        Side Effects:
            Reads the context's encrypted file under a cross-process lock and sleeps
            between retries.
        """
        persistence = self._build_persistence(context)
        raw: str | None = None
        for attempt in range(1, _READ_ATTEMPTS + 1):
            last = attempt == _READ_ATTEMPTS
            try:
                with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                    raw = persistence.load()
            except msal_extensions.persistence.PersistenceNotFound:
                if last:
                    return None
            except Exception as exc:
                if last:
                    raise _store_error(context, exc) from exc
            else:
                break
            time.sleep(self._read_retry_delay_seconds)
        assert raw is not None  # noqa: S101  # loop exits only via break (raw set) or return/raise
        if raw == "":  # the empty string is `delete_tokens`' "nothing stored" sentinel
            return None
        return _decode_token_bundle(raw)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing entry.

        Args:
            context: The context name to key the bundle by.
            tokens: The bundle to persist; JSON-encoded only for the persistence call.

        Returns:
            None.

        Raises:
            CredentialStoreError: The backend failed for any reason. Nothing is written
                to a plaintext fallback.

        Side Effects:
            Writes the context's encrypted file under a cross-process lock.
        """
        persistence = self._build_persistence(context)
        try:
            with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                persistence.save(_encode_token_bundle(tokens))
        except Exception as exc:
            raise _store_error(context, exc) from exc

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; safe when nothing is stored.

        Overwrites the entry with an empty string, which `get_tokens` reads as "nothing
        stored". The backend cannot tell an overwrite from a first write, so any
        exception means the backend is genuinely unusable.

        Args:
            context: The context name whose bundle is removed.

        Returns:
            None.

        Raises:
            CredentialStoreError: The backend failed for any reason.

        Side Effects:
            Overwrites the context's encrypted file under a cross-process lock.
        """
        persistence = self._build_persistence(context)
        try:
            with msal_extensions.CrossPlatLock(persistence.get_location() + ".lockfile"):
                persistence.save("")
        except Exception as exc:
            raise _store_error(context, exc) from exc


def _build_production_persistence(context: str) -> PersistenceBackend:
    """Build the encrypted persistence for `context`'s credential file.

    The file is `<config dir>/credentials/<context>.bin`. A module-level function (not
    inlined) so tests can monkeypatch it without changing `build_credential_store()`'s
    zero-argument signature.

    Args:
        context: The context name; becomes the file's base name.

    Returns:
        A `PersistenceBackend` encrypted with the operating system's secret store.

    Raises:
        PsCliError: On Linux, the OS Secret Service bindings (`python3-gi`,
            `gir1.2-secret-1`) are missing, so `msal-extensions` raised `ImportError` or
            `ValueError`. The hint is the library's own message, which names the
            install command; there is no plaintext fallback.

    Side Effects:
        Resolves the config directory; `msal-extensions` may probe the OS secret store.
    """
    location = str(resolve_config_dir() / "credentials" / f"{context}.bin")
    try:
        return cast(
            "PersistenceBackend",
            msal_extensions.build_encrypted_persistence(  # pyright: ignore[reportUnknownMemberType]  # msal-extensions ships no type information
                location
            ),
        )
    except (ImportError, ValueError) as exc:
        raise PsCliError(
            msg="the Linux credential-storage backend is missing a required system package",
            hint=str(exc),
        ) from exc


def build_credential_store() -> PersistenceCredentialStore:
    """Build the credential store ps-cli uses in production.

    Takes no arguments: the persistence factory resolves each context's location from
    the config directory itself.

    Returns:
        A `PersistenceCredentialStore` wired to the encrypted `msal-extensions`
        persistence; any storage failure later raises `CredentialStoreError` rather than
        falling back to a plaintext file.

    Raises:
        Nothing here; backend errors surface from the store's methods.

    Side Effects:
        None at construction.
    """
    return PersistenceCredentialStore(build_persistence=_build_production_persistence)
