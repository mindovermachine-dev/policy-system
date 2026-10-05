"""Credential view for the long-lived `ps-cli-mcp-bridge` process (issue #181).

`BridgeCredentialView` wraps a `ChangeAwareCredentialStore` and is handed to the
unchanged `device_flow.ensure_valid_access_token` as its `CredentialStore`. The bridge
calls `sync()` once per forwarded message: it stats the credential (cheap, no keychain
access) and only re-reads the keychain when the modification time moved. Between
changes `get_tokens()` answers from memory, so a valid cached access token plus an
unchanged credential means no keychain read (AC-BI-004). The very first `sync()` per
process always reads once, since a missing signal file does not prove nothing is stored.

Change signal: msal-extensions' `time_last_modified()` (the mtime of the
`credentials/<context>.bin` signal file, moved by every `save` on macOS, Linux and
Windows, `delete_tokens` included). The view records the mtime after each of its own
reads and writes; a different mtime means another process (`ps-cli auth login`/`logout`)
touched the credential.

Fail closed after seen (AC-BI-001/002): the view remembers whether a credential was ever
read or written for its context (`ever_seen`). Seen-then-gone (logout) makes the bridge
raise the "no stored credentials" error with the `ps-cli auth login` hint; never-seen (a
named local-test context with nothing to log into) stays unauthenticated. A re-login
(a different refresh token than the one last known to be in the store) is reported so the
bridge drops its cached access token (AC-BI-003).

Own writes: after a successful `set_tokens` the view records the post-write
modification time, so the bridge's own refresh does not trigger a pointless re-read.
Residual race (accepted, D-181-4): a foreign `ps-cli auth login` landing between our save
and our post-write stat is recorded as ours; the new refresh token is picked up at the
next change.

Read fallback (AC-BI-005): when a re-read fails with -67701 (`errSecInvalidRecord`) and a
credential is already held, the view keeps the in-memory credential, records the pre-read
modification time and logs one warning; every other read error propagates. Residual
(accepted): a login whose read fails with -67701 is missed until the next mtime change; the
old token keeps working until the IdP revokes it.

Best-effort persist (AC-BI-006): the rotated credential is held in memory before the
write is attempted; a failed write logs one warning and the call proceeds.
A late-completing abandoned save is not guarded by a compare-and-swap (that would need the
check inside `set_tokens`' cross-process lock, a `credentials.py` change out of scope): it
stores the rotated refresh token, still valid at the IdP, and the view self-heals on the
next mtime change through the `_last_persisted_rt` rule.

Bounded keychain calls (AC-BI-007): every store call runs on a daemon worker thread and
the caller waits at most `timeout_seconds` (5 s); a stall becomes a `CredentialStoreError`
for that message instead of blocking the single-threaded proxy loop. A timed-out
load/save thread is remembered as abandoned; while it is alive any new load/save fails
fast (it would only queue behind the abandoned call's cross-process lock for another
5 s). The stat is bounded too but never registered and never blocked by that guard (it
takes no lock), so a valid cache plus unchanged mtime keeps serving while a save is
stalled. Trade-off (accepted): a call that never returns blocks keychain access until the
host restarts the bridge. The worker only writes to its own result holder, never to
view state, so a late completion is discarded. A late-completing abandoned *save* still
lands in the store (see above); a late abandoned *read* only fills its discarded holder.

Secrets: the view logs and raises only the context name, exception types, integer
statuses and `PsCliError` msg/hint text; it never formats a `TokenBundle` or token value
(AC-BI-010). The refresh token lives only in process memory and in the credential store.

Single-threaded, one context per process; not safe for concurrent use.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_cli.credentials import ERR_SEC_INVALID_RECORD
from ps_cli.errors import CredentialStoreError, PsCliError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_cli.credentials import ChangeAwareCredentialStore, StoredRead, TokenBundle

_KEYCHAIN_CALL_TIMEOUT_SECONDS = 5.0
_AVAILABLE_HINT = "check that it is available and unlocked"


@dataclass
class _Outcome[T]:
    """Result holder a bounded worker fills; the caller reads it only after the Event."""

    value: T | None = None
    error: Exception | None = None


class BridgeCredentialView:
    """In-memory, change-aware `CredentialStore` over a `ChangeAwareCredentialStore`."""

    def __init__(
        self,
        store: ChangeAwareCredentialStore,
        *,
        logger: Callable[[str], None] | None = None,
        timeout_seconds: float = _KEYCHAIN_CALL_TIMEOUT_SECONDS,
    ) -> None:
        """Wrap `store`; nothing is read until the first `sync()`/`get_tokens()`.

        `logger` receives the view's secret-free warnings (the bridge injects its `_log`).
        """
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._abandoned: threading.Thread | None = None
        self._logger = logger
        self._initialized = False
        self._recorded_mtime: float | None = None
        self._last_known: TokenBundle | None = None
        self._ever_seen = False
        self._last_persisted_rt: str | None = None

    @property
    def holds_credential(self) -> bool:
        """True while the view holds a stored credential (a bundle read or written)."""
        return self._last_known is not None

    @property
    def ever_seen(self) -> bool:
        """True once any read or write yielded a bundle; never flips back (AC-BI-001/002).

        Tells "logged out under a running bridge" (fail closed) apart from "nothing was
        ever stored for this named context" (local-test: forward unauthenticated).
        """
        return self._ever_seen

    def sync(self, context: str) -> bool:
        """Refresh the in-memory credential if (and only if) the stored one changed.

        Returns True when the bridge must drop its cached access token (the credential
        vanished, or a foreign login replaced its refresh token).
        The first call reads unconditionally. Later calls stat the credential
        and re-read only when its modification time differs from the recorded one. The
        stat happens before the read and its value is what gets recorded, so a foreign
        write landing in between is re-read on the next message rather than missed.

        A `not_found` read after a credential was seen is treated as transient (a
        rewrite caught mid-flight): the last-known credential is kept and the mtime is
        left unrecorded so the next message retries. Only the empty logout sentinel
        means the credential is gone.
        """
        mtime = self._bounded(lambda: self._store.last_modified(context), context, guarded=False)
        if self._initialized and mtime == self._recorded_mtime:
            return False
        try:
            read = self._guarded_read(context)
        except CredentialStoreError as exc:
            if exc.status == ERR_SEC_INVALID_RECORD and self._last_known is not None:
                return self._fall_back_to_last_known(context, mtime)
            raise
        self._initialized = True
        if read.kind == "not_found" and self._ever_seen:
            return False
        self._recorded_mtime = mtime
        if read.bundle is None:
            return self._forget()
        return self._adopt(read.bundle)

    def _fall_back_to_last_known(self, context: str, mtime: float | None) -> bool:
        """A re-read failed with -67701: keep serving the in-memory credential (AC-BI-005).

        The pre-read `mtime` is recorded so later messages with an unchanged mtime do not
        re-read (and re-fail) every time; the warning is logged once per fallback event.
        Residual (accepted): a foreign login whose read fails with -67701 is missed until
        the next mtime change -- the old token keeps working until the IdP revokes it.
        """
        self._recorded_mtime = mtime
        self._warn(
            f"credential store read for context '{context}' failed with status "
            f"{ERR_SEC_INVALID_RECORD}; continuing with the last known credential"
        )
        return False

    def _guarded_read(self, context: str) -> StoredRead:
        """Read the credential from the store, bounded and honoring the abandoned guard."""
        return self._bounded(
            lambda: self._store.get_tokens_observed(context), context, guarded=True
        )

    def _bounded[T](self, call: Callable[[], T], context: str, *, guarded: bool) -> T:
        """Run `call` on a daemon thread; wait at most `timeout_seconds` (AC-BI-007).

        `guarded` calls (store load/save, which take the cross-process lock) fail fast
        while an earlier guarded call is still stalled, and register themselves as the
        abandoned thread on timeout. Unguarded calls (the lock-free stat) are bounded
        but never registered and never blocked by the guard (CHANGES F-3). The worker
        touches only its own `_Outcome`; a late result is discarded.
        """
        if guarded and self._abandoned is not None and self._abandoned.is_alive():
            raise self._stall_error(context, still_stalled=True)
        outcome: _Outcome[T] = _Outcome()
        done = threading.Event()

        def _work() -> None:
            try:
                outcome.value = call()
            except Exception as exc:  # noqa: BLE001  # relayed to the caller thread below
                outcome.error = exc
            finally:
                done.set()

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        if not done.wait(self._timeout_seconds):
            if guarded:
                self._abandoned = worker
            raise self._stall_error(context, still_stalled=False)
        if outcome.error is not None:
            raise outcome.error
        return cast("T", outcome.value)

    def _stall_error(self, context: str, *, still_stalled: bool) -> CredentialStoreError:
        """The secret-free credential-store error for a stalled or still-stalled call."""
        reason = (
            "a previous credential store call is still unresponsive"
            if still_stalled
            else f"the credential store did not respond within {self._timeout_seconds:g} s"
        )
        return CredentialStoreError(
            msg=f"could not access the credential store for context '{context}'",
            hint=f"{reason}; {_AVAILABLE_HINT}",
        )

    def _warn(self, message: str) -> None:
        """Log a secret-free warning through the injected logger, if any."""
        if self._logger is not None:
            self._logger(message)

    def _adopt(self, stored: TokenBundle) -> bool:
        """Take a freshly read bundle; True when it is a foreign login (drop the cache).

        Compared against `_last_persisted_rt`, the refresh token last known to be in the
        store: the same one means the store holds nothing new, so `_last_known` (possibly
        a newer in-memory rotation) is kept and the cache stays; a different one (or none
        held after a logout) means a foreign login, so the store wins. The first read of a
        process never drops (nothing cached yet that could be stale).
        """
        changed = self._last_known is None or stored.refresh_token != self._last_persisted_rt
        dropped = self._ever_seen and changed
        if changed:
            self._last_known = stored
            self._last_persisted_rt = stored.refresh_token
        self._ever_seen = True
        return dropped

    def _forget(self) -> bool:
        """The stored credential is empty/absent: forget it; True if one had been held."""
        dropped = self._last_known is not None
        self._last_known = None
        return dropped

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the last-known bundle from memory (syncing first if never synced)."""
        if not self._initialized:
            self.sync(context)
        return self._last_known

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Remember `tokens` in memory first, then persist them best-effort (AC-BI-006).

        The refresh already rotated at the IdP, so a persist failure must not fail the
        call (the old stored token may now be revoked, and the rotated one lives only in
        memory). On `PsCliError` one warning (the error's own msg/hint, no token) is
        logged, nothing is raised and the recorded mtime stays put. On success the
        post-write mtime is recorded so our own write does not trigger a re-read.
        `_last_persisted_rt` only advances on success, so a later foreign rewrite of the
        stale stored token is recognised as "nothing new" (CHANGES F-5).
        """
        self._last_known = tokens
        self._ever_seen = True
        try:
            self._bounded(lambda: self._store.set_tokens(context, tokens), context, guarded=True)
            self._last_persisted_rt = tokens.refresh_token
            self._recorded_mtime = self._bounded(
                lambda: self._store.last_modified(context), context, guarded=False
            )
        except PsCliError as exc:
            self._warn(
                f"could not persist the refreshed credential for context '{context}' "
                f"({exc.msg}; {exc.hint}); continuing with the in-memory credential"
            )
            return
        self._initialized = True

    def delete_tokens(self, context: str) -> None:
        """Delegate to the wrapped store (the bridge itself never deletes)."""
        self._bounded(lambda: self._store.delete_tokens(context), context, guarded=True)
