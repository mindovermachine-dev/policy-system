"""PostgreSQL persistence for `pending_approvals` (issue #131, PLAN.md §0.6/§1.1).

`PendingApprovalStore` is the `Protocol` the merge-gating call sites (a later
slice's `mcp_server.py`/`api/routes.py` wiring) depend on --
`PsycopgPendingApprovalStore` is the real implementation.

Connection strategy: one short-lived `psycopg.connect(...)` per call, opened
via `connect_from_config`, closed via `with` -- no pool. Mirrors
`ps_service.query_engine.falkordb_client.connect_from_config`'s
per-call-connection idiom exactly (see that module's docstring): the
`psycopg` constructor itself performs a real TCP+auth round trip, so a
connection failure is raised immediately by `connect()`/`connect_from_config`
themselves, unwrapped -- `check_connectivity_from_config` below is the one
place that wraps it into a domain-specific error for the startup/
dependency-health probe.
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
    """Persistence seam for `pending_approvals` rows.

    Constructor-injected wherever it is needed (no DI framework, L2's "plain
    constructor injection" rule) -- business logic depends on this
    `Protocol`, never on `PsycopgPendingApprovalStore` directly, so a test
    can substitute an in-memory fake.
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

        Computes the nonce, the high-entropy capability code, its `sha256`
        digest (`code_hash`), and `expires_at` (`created_at` + 15 minutes)
        internally -- the raw code is returned to the caller exactly once,
        here, so it can build the approval link (PLAN.md §0.5); it is never
        itself persisted.
        """
        ...

    def get_by_id(self, pending_approval_id: str) -> PendingApprovalRow | None:
        """Return the row with this `id`, or `None` if none exists."""
        ...

    def get_by_code_hash(self, code_hash: bytes) -> PendingApprovalRow | None:
        """Return the row whose `code_hash` matches, or `None` if none exists."""
        ...

    def mark_signed(self, pending_approval_id: str) -> bool:
        """Atomically flip `status` `'pending'` -> `'signed'`; return whether this call won.

        A single `UPDATE ... WHERE status = 'pending' RETURNING id` -- never
        a read-then-write -- so a concurrent second call against the same
        row loses the race outright (AC-BI-012/013): only one caller ever
        sees `True`.
        """
        ...

    def set_outcome(self, pending_approval_id: str, outcome: dict[str, object]) -> None:
        """Record the merge outcome (or a safe error message) onto an already-`'signed'` row.

        Called only after `mark_signed` has returned `True` for this same
        `pending_approval_id` (issue #131 Slice 3, PLAN.md §3).
        """
        ...


__all__ = [
    "PasskeySigningPostgresConnectionError",
    "PendingApprovalStore",
    "PsycopgPendingApprovalStore",
    "check_connectivity_from_config",
    "connect_from_config",
]

_CODE_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -- 256 bits (PLAN.md §0.5)
_NONCE_BYTES = 32  # secrets.token_bytes(32) -- PLAN.md §1.1's "fresh nonce"

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
    """Open a fresh `psycopg` connection from `config.passkey_signing_postgres_*`.

    Mirrors `ps_service.query_engine.falkordb_client.connect_from_config`'s
    per-call-connection idiom exactly: no pool, closed via `with` by the
    caller. Callers build `config` via `ps_service.config.load_config()`.
    """
    return psycopg.connect(
        host=config.passkey_signing_postgres_host,
        port=config.passkey_signing_postgres_port,
        dbname=config.passkey_signing_postgres_database,
        user=config.passkey_signing_postgres_user,
        password=config.passkey_signing_postgres_password,
    )


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Probe the Passkey Signing Postgres instance, if configured.

    Unlike LLM Interface (a hard-required dependency that marks itself
    unhealthy and raises when unconfigured), Passkey Signing Postgres is
    genuinely optional at this stage (PLAN.md §0.8: pilot scope, one gated
    tool) -- if `config.passkey_signing_postgres_host` is unset, this
    function does nothing at all, neither marking healthy nor unhealthy,
    mirroring `is_healthy`'s own "no recorded call yet is considered
    healthy" default. This is what keeps an environment that never
    configures Postgres from showing a perpetual false alarm in `/ready`'s
    `unhealthy_dependencies`.

    Raises:
        PasskeySigningPostgresConnectionError: the instance is configured
            but unreachable (connection failure or the round-trip query
            itself fails); the outcome is also recorded in
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
    """Map one raw `psycopg` result row (fixed column order, see `_SELECT_COLUMNS`) to a row.

    `cast()` is unavoidable at exactly this one boundary (L2's cast() policy):
    `psycopg`'s tuple-row result carries no per-column static type without a
    per-query `Row` type parameter, which is not worth introducing for two
    call sites. Every column's expected Python type is fixed by
    `migrations/0001_pending_approvals.sql`'s own schema.
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
    """Real `PendingApprovalStore` backed by PostgreSQL via `psycopg[binary]` (PLAN.md §0.6).

    Every method opens, uses, and closes its own connection (`with
    connect_from_config(self._config) as conn`) -- no pool, no cached
    connection held across calls.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Store `config`; no connection is opened until a method is called."""
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

        `expires_at` is computed by `migrations/0001_pending_approvals.sql`'s
        own column default (`now() + interval '15 minutes'`), not in Python
        -- both `created_at` and `expires_at` resolve from the same
        statement-local `now()` snapshot, so the two stay exactly 15 minutes
        apart.
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
        """Return the row with this `id`, or `None` if none exists."""
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
        """Return the row whose `code_hash` matches, or `None` if none exists."""
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
        """Atomically flip `status` `'pending'` -> `'signed'`; return whether this call won.

        Issue #131 Slice 3 (PLAN.md §3, AC-BI-012/013): a single
        `UPDATE ... WHERE status = 'pending' RETURNING id`, its own
        commit -- never a read-then-write -- so a concurrent second call
        against the same row loses the race outright: `cur.rowcount`/the
        returned record is empty for every caller but the one that actually
        flipped the row.
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
        """Record the merge outcome (or a safe error message) onto an already-`'signed'` row.

        Called only after `mark_signed` has returned `True` for this same
        `pending_approval_id` (issue #131 Slice 3, PLAN.md §3 step (c)).
        """
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SET_OUTCOME, {"id": pending_approval_id, "outcome": Json(outcome)})
                conn.commit()
        except psycopg.Error as exc:
            raise PendingApprovalPersistenceError(
                f"failed to record outcome for pending approval {pending_approval_id!r}: {exc}"
            ) from exc
