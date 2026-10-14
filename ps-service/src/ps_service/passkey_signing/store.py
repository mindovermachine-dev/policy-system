"""PostgreSQL persistence for `pending_approvals`, the passkey-approval records behind merges.

`PendingApprovalStore` is the `Protocol` that the merge-gating call sites depend on;
`PsycopgPendingApprovalStore` is the real implementation. `connect_from_config` and
`check_connectivity_from_config` open and probe the passkey-signing Postgres instance.

Connection strategy: one short-lived `psycopg` connection per call, opened by
`connect_from_config` and closed by a `with` block; there is no pool. Opening a connection
performs a real TCP and authentication round trip, so `connect_from_config` itself raises
`psycopg.Error` on failure, unwrapped. Store methods translate that into
`PendingApprovalPersistenceError`; `check_connectivity_from_config` translates it into
`PasskeySigningPostgresConnectionError` for the startup and dependency-health probe.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Protocol, cast

import psycopg
from psycopg.types.json import Json

from ps_service.dependency_health import PASSKEY_SIGNING_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.passkey_signing.errors import (
    PasskeySigningPostgresConnectionError,
    PendingApprovalPersistenceError,
)
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from typing import Literal

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig


class PendingApprovalStore(Protocol):
    """Persistence seam for `pending_approvals` rows: create, look up, sign and record outcome.

    Constructor-injected into business logic, which depends on this `Protocol` and never on
    `PsycopgPendingApprovalStore` directly, so a test can substitute an in-memory fake. Every
    method raises `PendingApprovalPersistenceError` when the Postgres call fails.
    """

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        """Insert a new pending approval; return the row plus the raw, unhashed code.

        The store generates the nonce, a high-entropy one-time capability code and its SHA-256
        digest (`code_hash`); the approval expires 15 minutes after creation. Only the digest is
        persisted: the raw code is returned here exactly once so the caller can build the
        approval link.

        Args:
            tool_name: Name of the gated tool the approval authorises.
            normalized_args: Canonical arguments of that tool call, stored as JSON.
            actor_subject: Identity-provider subject of the requesting caller.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            display_summary: Human-readable facts shown to the approver, stored as JSON.

        Returns:
            A tuple of the stored `PendingApprovalRow` (status `"pending"`) and the raw
            one-time code.

        Raises:
            PendingApprovalPersistenceError: the insert failed or the database connection could
                not be opened. Nothing is stored.

        Side Effects:
            Opens a connection, inserts and commits one `pending_approvals` row.
        """
        ...

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        """Return the approval row with this id.

        Args:
            pending_approval_id: The row id (UUID string).

        Returns:
            The `PendingApprovalRow`, or `None` if no row has that id.

        Raises:
            PendingApprovalPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
        ...

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        """Return the approval row whose one-time code has this SHA-256 digest.

        Args:
            code_hash: SHA-256 digest of the raw one-time code presented by the approver.

        Returns:
            The matching `PendingApprovalRow`, or `None` if no row matches.

        Raises:
            PendingApprovalPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
        ...

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Atomically flip an approval's status from `'pending'` to `'signed'`.

        Implemented as a single conditional `UPDATE`, never a read followed by a write, so when
        callers race on the same row exactly one of them gets `True`.

        Args:
            pending_approval_id: The row id (UUID string).

        Returns:
            `True` if this call changed the row from pending to signed; `False` if no pending row
            with that id existed (unknown id, or already signed).

        Raises:
            PendingApprovalPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, updates and commits at most one row.
        """
        ...

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        """Record the merge outcome (or a safe error message) on an already-signed approval.

        Call only after `mark_signed` returned `True` for the same id.

        Args:
            pending_approval_id: The row id (UUID string).
            outcome: JSON-serialisable result: the merge's `winner_id` and `loser_id`, or an
                `error` message safe to show the caller. Replaces any earlier outcome.

        Returns:
            None.

        Raises:
            PendingApprovalPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, sets the `outcome` column of the row and commits. An unknown id
            updates nothing and raises nothing.
        """
        ...


__all__ = [
    "PasskeySigningPostgresConnectionError",
    "PendingApprovalStore",
    "PsycopgPendingApprovalStore",
    "check_connectivity_from_config",
    "connect_from_config",
]

_CODE_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -- 256 bits
_NONCE_BYTES = 32  # secrets.token_bytes(32) -- a fresh random nonce per approval

_INSERT_PENDING_APPROVAL = """
INSERT INTO pending_approvals (
    code_hash, tool_name, normalized_args, actor_subject, actor_issuer, nonce, display_summary
) VALUES (
    %(code_hash)s, %(tool_name)s, %(normalized_args)s, %(actor_subject)s, %(actor_issuer)s,
    %(nonce)s, %(display_summary)s
)
RETURNING
    id, code_hash, tool_name, normalized_args, actor_subject, actor_issuer, nonce,
    display_summary, status, outcome, created_at, expires_at
"""

_SELECT_COLUMNS = (
    "id, code_hash, tool_name, normalized_args, actor_subject, actor_issuer, nonce, "
    "display_summary, status, outcome, created_at, expires_at"
)
_SELECT_BY_ID = f"SELECT {_SELECT_COLUMNS} FROM pending_approvals WHERE id = %(id)s"  # noqa: S608 - fixed literal, no interpolated user input
_SELECT_BY_CODE_HASH = (
    f"SELECT {_SELECT_COLUMNS} FROM pending_approvals WHERE code_hash = %(code_hash)s"  # noqa: S608 - fixed literal, no interpolated user input
)
_MARK_SIGNED = """
UPDATE pending_approvals SET status = 'signed'
WHERE id = %(id)s AND status = 'pending'
RETURNING id
"""
_SET_OUTCOME = """
UPDATE pending_approvals SET outcome = %(outcome)s
WHERE id = %(id)s
"""


def connect_from_config(config: ServiceConfig) -> psycopg.Connection[TupleRow]:
    """Open a new connection to the passkey-signing Postgres instance.

    Args:
        config: Service configuration; its `passkey_signing_postgres_*` host, port, database,
            user and password are used.

    Returns:
        An open connection. There is no pool: the caller closes it, normally with a `with`
        block.

    Raises:
        psycopg.Error: the connection could not be established (unreachable host, bad
            credentials); raised unwrapped.

    Side Effects:
        Opens a network connection to Postgres.
    """
    return psycopg.connect(
        host=config.passkey_signing_postgres_host,
        port=config.passkey_signing_postgres_port,
        dbname=config.passkey_signing_postgres_database,
        user=config.passkey_signing_postgres_user,
        password=config.passkey_signing_postgres_password,
    )


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Probe the passkey-signing Postgres instance, if one is configured.

    The instance is optional: when `config.passkey_signing_postgres_host` is unset the function
    does nothing, recording neither healthy nor unhealthy. A dependency with no recorded call is
    treated as healthy, so an environment that never configures passkey signing does not show a
    permanent false alarm in the readiness endpoint's unhealthy dependencies.

    Args:
        config: Service configuration holding the `passkey_signing_postgres_*` settings.

    Returns:
        None.

    Raises:
        PasskeySigningPostgresConnectionError: the instance is configured but unreachable (the
            connection fails or the `SELECT 1` round trip fails). The message names the host and
            port and includes the driver error.

    Side Effects:
        When configured, opens a connection and runs `SELECT 1`, then records the
        passkey-signing Postgres dependency as healthy on success or unhealthy on failure in
        `ps_service.dependency_health`.
    """
    if config.passkey_signing_postgres_host is None:
        return
    try:
        with connect_from_config(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg.Error as exc:
        mark_unhealthy(PASSKEY_SIGNING_POSTGRES, error=exc)
        raise PasskeySigningPostgresConnectionError(
            "Passkey Signing Postgres connection failed at "
            f"{config.passkey_signing_postgres_host}:{config.passkey_signing_postgres_port}. "
            f"Is Postgres running? Error: {exc}"
        ) from exc
    mark_healthy(PASSKEY_SIGNING_POSTGRES)


def _row_from_record(record: Sequence[object]) -> PendingApprovalRow:
    """Map one raw `psycopg` result row (column order `_SELECT_COLUMNS`) to a `PendingApprovalRow`.

    `cast()` is needed at this single boundary because a tuple-row result has no per-column static
    type; each column's Python type is fixed by `migrations/0001_pending_approvals.sql`.
    """
    (
        row_id,
        code_hash,
        tool_name,
        normalized_args,
        actor_subject,
        actor_issuer,
        nonce,
        display_summary,
        status,
        outcome,
        created_at,
        expires_at,
    ) = record
    return PendingApprovalRow(
        id=str(row_id),
        code_hash=cast("bytes", code_hash),
        tool_name=cast("str", tool_name),
        normalized_args=cast("dict[str, object]", normalized_args),
        actor_subject=cast("str", actor_subject),
        actor_issuer=cast("str", actor_issuer),
        nonce=cast("bytes", nonce),
        display_summary=cast("dict[str, object]", display_summary),
        status=cast('Literal["pending", "signed"]', status),
        outcome=cast("dict[str, object] | None", outcome),
        created_at=cast("datetime", created_at),
        expires_at=cast("datetime", expires_at),
    )


class PsycopgPendingApprovalStore:
    """`PendingApprovalStore` backed by PostgreSQL via `psycopg`.

    Every method opens, uses and closes its own connection (no pool, no connection held across
    calls) and raises `PendingApprovalPersistenceError` on any driver failure.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Keep `config` for later connections; opens none.

        Args:
            config: Service configuration holding the `passkey_signing_postgres_*` settings.
        """
        self._config = config

    def create_pending_approval(
        self,
        *,
        tool_name: str,
        normalized_args: dict[str, object],
        actor_subject: str,
        actor_issuer: str,
        display_summary: dict[str, object],
    ) -> tuple[PendingApprovalRow, str]:
        """Insert a new pending approval; return the row plus the raw, unhashed code.

        The nonce, the one-time code (256 bits) and the code's SHA-256 digest are generated here;
        only the digest is stored. `expires_at` comes from the column default in
        `migrations/0001_pending_approvals.sql` (`now() + interval '15 minutes'`), so
        `created_at` and `expires_at` derive from the same `now()` and stay exactly 15 minutes
        apart.

        Args:
            tool_name: Name of the gated tool the approval authorises.
            normalized_args: Canonical arguments of that tool call, stored as JSON.
            actor_subject: Identity-provider subject of the requesting caller.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            display_summary: Human-readable facts shown to the approver, stored as JSON.

        Returns:
            A tuple of the stored `PendingApprovalRow` (status `"pending"`) and the raw
            one-time code, which is not retrievable afterwards.

        Raises:
            PendingApprovalPersistenceError: the insert failed, the connection could not be
                opened, or no row was returned. Nothing is stored.

        Side Effects:
            Opens a connection, inserts and commits one `pending_approvals` row.
        """
        code = secrets.token_urlsafe(_CODE_TOKEN_BYTES)
        code_hash = hashlib.sha256(code.encode()).digest()
        nonce = secrets.token_bytes(_NONCE_BYTES)
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _INSERT_PENDING_APPROVAL,
                    {
                        "code_hash": code_hash,
                        "tool_name": tool_name,
                        "normalized_args": Json(normalized_args),
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "nonce": nonce,
                        "display_summary": Json(display_summary),
                    },
                )
                record = cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to create a pending approval: {exc}"
            ) from exc
        if record is None:  # pragma: no cover - INSERT...RETURNING always yields exactly one row
            message = "INSERT...RETURNING for pending_approvals unexpectedly returned no row"
            raise PendingApprovalPersistenceError(message)
        return _row_from_record(record), code

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        """Return the approval row with this id.

        Args:
            pending_approval_id: The row id (UUID string).

        Returns:
            The `PendingApprovalRow`, or `None` if no row has that id.

        Raises:
            PendingApprovalPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_BY_ID, {"id": pending_approval_id})
                record = cur.fetchone()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to look up pending approval {pending_approval_id!r}: {exc}"
            ) from exc
        return _row_from_record(record) if record is not None else None

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        """Return the approval row whose one-time code has this SHA-256 digest.

        Args:
            code_hash: SHA-256 digest of the raw one-time code presented by the approver.

        Returns:
            The matching `PendingApprovalRow`, or `None` if no row matches.

        Raises:
            PendingApprovalPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_BY_CODE_HASH, {"code_hash": code_hash})
                record = cur.fetchone()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to look up a pending approval by code_hash: {exc}"
            ) from exc
        return _row_from_record(record) if record is not None else None

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Atomically flip an approval's status from `'pending'` to `'signed'`.

        A single `UPDATE ... WHERE status = 'pending' RETURNING id` with its own commit, never a
        read followed by a write, so a concurrent second call on the same row gets no returned
        record and loses the race outright.

        Args:
            pending_approval_id: The row id (UUID string).

        Returns:
            `True` if this call changed the row from pending to signed; `False` if no pending row
            with that id existed (unknown id, or already signed).

        Raises:
            PendingApprovalPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, updates and commits at most one row.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_MARK_SIGNED, {"id": pending_approval_id})
                record = cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to mark pending approval {pending_approval_id!r} as signed: {exc}"
            ) from exc
        return record is not None

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        """Record the merge outcome (or a safe error message) on an already-signed approval.

        Call only after `mark_signed` returned `True` for the same id.

        Args:
            pending_approval_id: The row id (UUID string).
            outcome: JSON-serialisable result: the merge's `winner_id` and `loser_id`, or an
                `error` message safe to show the caller. Replaces any earlier outcome.

        Returns:
            None.

        Raises:
            PendingApprovalPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, sets the `outcome` column of the row and commits. An unknown id
            updates nothing and raises nothing.
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SET_OUTCOME, {"id": pending_approval_id, "outcome": Json(outcome)})
                conn.commit()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to record outcome for pending approval {pending_approval_id!r}: {exc}"
            ) from exc
