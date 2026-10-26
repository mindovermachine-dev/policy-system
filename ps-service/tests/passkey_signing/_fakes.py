"""Shared test doubles for the ``ps_service.passkey_signing`` test package.

``tests/passkey_signing/`` is an importable package (it has an
``__init__.py``), so its per-file test modules -- and later slices' MCP/REST
wiring tests -- share this hand-written double from here instead of
redeclaring it, mirroring ``tests/api/_fakes.py``/``tests/curated_source/
_fakes.py``'s own established convention.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ps_service.passkey_signing.errors import SigningRevocationPersistenceError
from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow
from ps_service.passkey_signing.revocation import ActorSigningRevocation, ActorSigningState

if TYPE_CHECKING:
    from collections.abc import Callable

_CODE_TOKEN_BYTES = 32  # mirrors `store.py`'s own `_CODE_TOKEN_BYTES`
_NONCE_BYTES = 32  # mirrors `store.py`'s own `_NONCE_BYTES`
_EXPIRY_WINDOW = timedelta(minutes=15)  # fixed, matches the migration's SQL default


@dataclass
class FakePendingApprovalStore:
    """In-memory `PendingApprovalStore` (structural `Protocol` match, no real Postgres).

    Mirrors `PsycopgPendingApprovalStore`'s own code/nonce/expiry generation
    exactly (`ps_service.passkey_signing.store`), so a test exercising this
    fake proves the same contract the real store must uphold, without a live
    Postgres.
    """

    _rows_by_id: dict[str, PendingApprovalRow] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    """Guards `mark_signed`'s check-then-write: the real
    `PsycopgPendingApprovalStore.mark_signed` gets its atomicity from a single
    `UPDATE ... WHERE status = 'pending'` SQL statement; this fake has no such
    single-statement primitive, so it needs its own lock to genuinely honor
    the same `PendingApprovalStore.mark_signed` contract ("only one caller
    ever sees `True`") under a real concurrent-threads test
    (`test_signing_hardening.py`), not just under sequential calls."""

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        """Mint a code/nonce/id, store a `'pending'` row, and return it plus the raw code."""
        code = secrets.token_urlsafe(_CODE_TOKEN_BYTES)
        code_hash = hashlib.sha256(code.encode()).digest()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        created_at = datetime.now(UTC)
        row = PendingApprovalRow(
            id=str(uuid.uuid4()),
            code_hash=code_hash,
            tool_name=tool_name,
            normalized_args=normalized_args,
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            nonce=nonce,
            display_summary=display_summary,
            status="pending",
            outcome=None,
            created_at=created_at,
            expires_at=created_at + _EXPIRY_WINDOW,
        )
        self._rows_by_id[row.id] = row
        return row, code

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        """Return the row with this `id`, or `None` if none exists."""
        return self._rows_by_id.get(pending_approval_id)

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        """Return the row whose `code_hash` matches, or `None` if none exists."""
        for row in self._rows_by_id.values():
            if row.code_hash == code_hash:
                return row
        return None

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Atomically (single-threaded fake) flip `'pending'` -> `'signed'`.

        Mirrors `PsycopgPendingApprovalStore.mark_signed`'s
        contract: returns `False` (no mutation) if the row is unknown or
        already not `'pending'`, `True` otherwise.
        """
        row = self._rows_by_id.get(pending_approval_id)
        if row is None or row.status != "pending":
            return False
        self._rows_by_id[pending_approval_id] = replace(row, status="signed")
        return True

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        """Record the merge outcome (or a safe error message) onto the row."""
        row = self._rows_by_id.get(pending_approval_id)
        if row is not None:
            self._rows_by_id[pending_approval_id] = replace(row, outcome=outcome)

    def all_rows(self) -> tuple[PendingApprovalRow, ...]:
        """Every stored approval row, in creation order."""
        return tuple(self._rows_by_id.values())

    def replace_rows(self, rows: tuple[PendingApprovalRow, ...]) -> None:
        """Replace the stored rows wholesale (a revocation fake's void and rollback)."""
        self._rows_by_id = {row.id: row for row in rows}


@dataclass
class FakeSigningCredentialStore:
    """In-memory `SigningCredentialStore` (structural `Protocol` match, no real Postgres).

    Mirrors `FakePendingApprovalStore`'s own shape.
    """

    _rows: list[SigningCredentialRow] = field(default_factory=list)

    def create_signing_credential(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        rp_id: str | None,
    ) -> SigningCredentialRow:
        """Insert a newly-enrolled credential; return the created row."""
        row = SigningCredentialRow(
            id=str(uuid.uuid4()),
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            created_at=datetime.now(UTC),
            rp_id=rp_id,
        )
        self._rows.append(row)
        return row

    def list_for_actor(
        self, *, actor_subject: str, actor_issuer: str, rp_id: str
    ) -> tuple[SigningCredentialRow, ...]:
        """Return this actor's credentials enrolled under `rp_id`.

        A `None` `rp_id` row never matches, mirroring the real store's
        `rp_id = %(rp_id)s` three-valued-logic behaviour.
        """
        return tuple(
            row
            for row in self._rows
            if row.actor_subject == actor_subject
            and row.actor_issuer == actor_issuer
            and row.rp_id == rp_id
        )

    def has_any_for_actor(self, *, actor_subject: str, actor_issuer: str, rp_id: str) -> bool:
        """Return whether at least one credential is enrolled for this actor under `rp_id`."""
        return bool(
            self.list_for_actor(actor_subject=actor_subject, actor_issuer=actor_issuer, rp_id=rp_id)
        )

    def get_by_credential_id(self, credential_id: bytes) -> SigningCredentialRow | None:
        """Return the row whose `credential_id` matches, or `None` if none exists."""
        for row in self._rows:
            if row.credential_id == credential_id:
                return row
        return None

    def update_sign_count(self, *, credential_id: bytes, sign_count: int) -> None:
        """Persist the authenticator's latest reported `sign_count`."""
        for index, row in enumerate(self._rows):
            if row.credential_id == credential_id:
                self._rows[index] = replace(row, sign_count=sign_count)
                return

    def all_rows(self) -> tuple[SigningCredentialRow, ...]:
        """Every stored credential row, in enrolment order."""
        return tuple(self._rows)

    def replace_rows(self, rows: tuple[SigningCredentialRow, ...]) -> None:
        """Replace the stored rows wholesale (a revocation fake's delete and rollback)."""
        self._rows = list(rows)


@dataclass
class FakeSigningRevocationStore:
    """In-memory `SigningRevocationStore` over the credential and approval fakes.

    `revoke_for_actor` first voids the actor's pending approvals in `pending` (every one, expired
    or not: status `signed`, outcome `void_outcome`), then deletes the actor's credentials from
    `credentials` (every `rp_id`, the empty one included), calls `before_commit` with the result,
    and on any exception from it restores the snapshots taken before the changes, as the real
    store's transaction rolls back. `fail_before_audit` raises the store error before
    `before_commit` runs; `fail_commit` raises it after `before_commit` returned, restoring the
    snapshots (a failed COMMIT); `fail_read` makes `state_for_actor` raise it.
    """

    credentials: FakeSigningCredentialStore
    pending: FakePendingApprovalStore = field(default_factory=FakePendingApprovalStore)
    fail_before_audit: bool = False
    fail_commit: bool = False
    fail_read: bool = False

    def state_for_actor(self, *, actor_subject: str, actor_issuer: str) -> ActorSigningState:
        """Count the actor's credentials and list its pending approval ids, oldest first."""
        if self.fail_read:
            message = "fake signing store unavailable"
            raise SigningRevocationPersistenceError(message)
        credentials = sum(
            1 for row in self.credentials.all_rows() if _owned(row, actor_subject, actor_issuer)
        )
        return ActorSigningState(
            credentials=credentials,
            pending_approval_ids=self._pending_ids(actor_subject, actor_issuer),
        )

    def revoke_for_actor(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        void_outcome: dict[str, object],
        before_commit: Callable[[ActorSigningRevocation], None],
    ) -> ActorSigningRevocation:
        """Void the actor's approvals and delete its credentials; keep both only on success."""
        if self.fail_before_audit:
            message = "fake signing store unavailable"
            raise SigningRevocationPersistenceError(message)
        credential_snapshot = self.credentials.all_rows()
        pending_snapshot = self.pending.all_rows()
        voided = self._pending_ids(actor_subject, actor_issuer)
        self.pending.replace_rows(
            tuple(
                replace(row, status="signed", outcome=void_outcome) if row.id in voided else row
                for row in pending_snapshot
            )
        )
        kept = tuple(
            row for row in credential_snapshot if not _owned(row, actor_subject, actor_issuer)
        )
        result = ActorSigningRevocation(
            credentials_deleted=len(credential_snapshot) - len(kept), voided_approval_ids=voided
        )
        self.credentials.replace_rows(kept)
        try:
            before_commit(result)
        except BaseException:
            self._restore(credential_snapshot, pending_snapshot)
            raise
        if self.fail_commit:
            self._restore(credential_snapshot, pending_snapshot)
            message = "fake commit failed"
            raise SigningRevocationPersistenceError(message)
        return result

    def _restore(
        self,
        credentials: tuple[SigningCredentialRow, ...],
        pending: tuple[PendingApprovalRow, ...],
    ) -> None:
        self.credentials.replace_rows(credentials)
        self.pending.replace_rows(pending)

    def _pending_ids(self, actor_subject: str, actor_issuer: str) -> tuple[str, ...]:
        return tuple(
            row.id
            for row in sorted(self.pending.all_rows(), key=lambda row: row.created_at)
            if _owned(row, actor_subject, actor_issuer) and row.status == "pending"
        )


def _owned(row: SigningCredentialRow | PendingApprovalRow, subject: str, issuer: str) -> bool:
    return row.actor_subject == subject and row.actor_issuer == issuer
