"""PostgreSQL persistence for the shared, insert-only `audit_events` table (issue #147).

`AuditStore` is the `Protocol` every `ps_service` component that emits audit
events depends on -- `PsycopgAuditStore` is the real implementation. Not
nested under any one consumer component, even though `audit_events` lives in
the PS state Postgres instance that several components share. This
component reuses `ps_service.persistence.connect_from_config` directly for
its own connection lifecycle (`record_standalone`/`query`) rather than
duplicating a parallel `PS_AUDIT_POSTGRES_*` config surface; the table
itself is created by this component's own migration directory, applied by
the shared `ps_service.persistence` runner.

Slice 1 shipped `record` only -- the cursor-scoped write every in-transaction
caller (Slice 2's store repoint) needs. Slice 3 added
`record_standalone`: the denial-recording write with no surrounding
state-changing transaction to join (access-denied / self-grant-or-revoke-
blocked / SystemOwner-floor-violation -- these are raised in
the calling service *before* any state-changing store method is ever
called, so there is no open cursor for a `record` call to reuse). Slice 4
adds `query` below: the `list-audit-events` read path -- newest-first,
keyset-paginated, filtered.

Insert-only is enforced by omission, not a DB privilege/trigger (that is
issue #151, out of scope here): `AuditStore` never defines an `UPDATE`/
`DELETE` method at all (AC-BI-013).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast

import psycopg
from psycopg.types.json import Json
from pydantic import ValidationError

from ps_service.audit.errors import (
    AuditInvalidCursorError,
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditUnknownActionError,
)
from ps_service.audit.models import AuditEventRow, AuditQueryPage, resolve_details_model
from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.persistence import StatePostgresConnectionError, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Literal, LiteralString

    from psycopg.rows import TupleRow

    from ps_service.audit.models import AuditQueryFilters
    from ps_service.config import ServiceConfig

_AUDIT_STORE_UNAVAILABLE_MESSAGE = "The audit store is temporarily unavailable."
_INVALID_CURSOR_MESSAGE = "audit query cursor is malformed"
_CURSOR_FIELD_SEPARATOR = "|"
_CURSOR_FIELD_COUNT = 2  # "{occurred_at_iso}|{id}" -- exactly two `|`-separated fields

_INSERT_AUDIT_EVENT = """
INSERT INTO audit_events (
    actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
) VALUES (
    %(actor_subject)s, %(actor_issuer)s, %(action)s, %(resource_type)s, %(resource_id)s,
    %(outcome)s, %(details)s
)
"""

_SELECT_AUDIT_EVENTS_COLUMNS = (
    "id, occurred_at, actor_subject, actor_issuer, action, resource_type, resource_id, "
    "outcome, details"
)


def _encode_cursor(*, occurred_at: datetime, event_id: str) -> str:
    """Opaque `next_cursor` value: base64 of `"{occurred_at_iso}|{id}"` (PLAN.md §3.4).

    The caller (`list-audit-events`) must never document or rely on this
    internal shape -- only that it round-trips through `query`'s own
    `cursor` argument.
    """
    raw = f"{occurred_at.isoformat()}{_CURSOR_FIELD_SEPARATOR}{event_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode and validate a `cursor` string, raising `AuditInvalidCursorError` on any defect.

    Covers every malformed shape PLAN.md §3.4 names: not valid base64, the
    wrong field count once decoded, an unparseable timestamp, or a
    non-UUID id.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise AuditInvalidCursorError(_INVALID_CURSOR_MESSAGE) from exc
    parts = raw.split(_CURSOR_FIELD_SEPARATOR)
    if len(parts) != _CURSOR_FIELD_COUNT:
        raise AuditInvalidCursorError(_INVALID_CURSOR_MESSAGE)
    occurred_at_raw, event_id = parts
    try:
        occurred_at = datetime.fromisoformat(occurred_at_raw)
    except ValueError as exc:
        raise AuditInvalidCursorError(_INVALID_CURSOR_MESSAGE) from exc
    try:
        uuid.UUID(event_id)
    except ValueError as exc:
        raise AuditInvalidCursorError(_INVALID_CURSOR_MESSAGE) from exc
    return occurred_at, event_id


_EQUALITY_FILTER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("actor_subject", "actor_subject"),
    ("actor_issuer", "actor_issuer"),
    ("resource_type", "resource_type"),
    ("resource_id", "resource_id"),
    ("action", "action"),
)
"""`(AuditQueryFilters` field name, SQL column name)` pairs sharing one `col = %(name)s`
equality-fragment shape -- every filter except the two range/cursor comparisons below,
which use `>=`/`<=`/`<` instead of `=` and so don't fit this same data-driven loop."""


def _build_query_conditions(
    filters: AuditQueryFilters, *, cursor_bound: tuple[datetime, str] | None, page_size: int
) -> tuple[list[str], dict[str, object]]:
    """Build `query`'s dynamic `WHERE` fragments and their parameterized values.

    Data-driven (`_EQUALITY_FILTER_COLUMNS`) rather than one `if` per filter,
    both to keep `PsycopgAuditStore.query`'s own cyclomatic complexity down
    (ruff C901) and to avoid this function repeating the same complexity
    itself. Every fragment is still a fixed literal string; only *which*
    fragments are included is runtime-conditional. Every actual filter value
    flows through the returned `params` dict, never interpolated into a
    fragment.
    """
    conditions: list[str] = []
    params: dict[str, object] = {"fetch_limit": page_size + 1}
    for field_name, column in _EQUALITY_FILTER_COLUMNS:
        value = getattr(filters, field_name)
        if value is not None:
            conditions.append(f"{column} = %({field_name})s")
            params[field_name] = value
    if filters.occurred_from is not None:
        conditions.append("occurred_at >= %(occurred_from)s")
        params["occurred_from"] = filters.occurred_from
    if filters.occurred_to is not None:
        conditions.append("occurred_at <= %(occurred_to)s")
        params["occurred_to"] = filters.occurred_to
    if cursor_bound is not None:
        conditions.append("(occurred_at, id) < (%(cursor_occurred_at)s, %(cursor_id)s)")
        params["cursor_occurred_at"] = cursor_bound[0]
        params["cursor_id"] = cursor_bound[1]
    return conditions, params


def _row_from_record(record: Sequence[object]) -> AuditEventRow:
    """Map one raw `psycopg` result row (fixed column order, `_SELECT_AUDIT_EVENTS_COLUMNS`).

    `cast()` at this one boundary mirrors the other `psycopg`-backed
    stores' own `_row_from_record` (L2's `cast()` policy) -- every column's expected
    Python type is fixed by `migrations/0001_audit_events.sql`'s own schema.
    `id` is converted with `str()`, not `cast()`, since `psycopg` returns a
    `uuid.UUID` object for a `uuid` column, mirroring
    `ps_service.passkey_signing.store`'s own `id=str(row_id)` convention.
    """
    (
        row_id,
        occurred_at,
        actor_subject,
        actor_issuer,
        action,
        resource_type,
        resource_id,
        outcome,
        details,
    ) = record
    return AuditEventRow(
        id=str(row_id),
        occurred_at=cast("datetime", occurred_at),
        actor_subject=cast("str", actor_subject),
        actor_issuer=cast("str", actor_issuer),
        action=cast("str", action),
        resource_type=cast("str", resource_type),
        resource_id=cast("str", resource_id),
        outcome=cast('Literal["applied", "rejected", "failed"]', outcome),
        details=cast("dict[str, object]", details),
    )


class AuditStore(Protocol):
    """Persistence seam for `audit_events` rows.

    Constructor-injected wherever it is needed (no DI framework, L2's "plain
    constructor injection" rule) -- every consumer store (e.g. the
    access-role store) depends on this `Protocol`, never on
    `PsycopgAuditStore` directly, so a test can substitute an in-memory fake.
    """

    def record(
        self,
        cur: psycopg.Cursor[TupleRow],
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Validate `details` against `action`'s registered model, then `INSERT` one row.

        Uses the CALLER's own already-open cursor/transaction, never a
        connection this method opens itself -- so this insert commits or
        rolls back atomically together with whatever state change `cur`'s
        surrounding transaction is also performing (AC-BI-006/AC-BI-010).
        Deliberately does not catch `psycopg.Error` itself (it owns no
        transaction to roll back) -- propagates verbatim to the caller's own
        `except psycopg.Error` block, mirroring how every
        consumer store method already relies on its own `with
        connect_from_config(...) as conn:` rollback-on-exception behavior.

        Raises:
            AuditUnknownActionError: `action` has no registered model
                (AC-BI-005).
            AuditInvalidDetailsError: `details` fails that model's
                validation (AC-BI-005/AC-BI-009).
        """
        ...

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Open its own connection/transaction, call `record`, commit (issue #147, Slice 3).

        For a denial that has no surrounding state-changing transaction to
        join (access-denied / self-grant-or-revoke-blocked /
        SystemOwner-floor-violation, AC-BI-012's three RBAC/rule denials --
        raised by the calling service *before* any state-changing store
        method is ever called, so nothing else is being written and
        there is no cursor-scoped transaction for `record` to join).

        Raises:
            AuditPostgresUnavailableError: the connection could not be
                opened (AC-BI-011) -- message carries no host/port/driver
                detail.
            AuditPersistenceError: the connection opened but the underlying
                `INSERT` failed.
            AuditUnknownActionError / AuditInvalidDetailsError: as `record`.
        """
        ...

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Newest-first, keyset-paginated read of `audit_events` (issue #147, Slice 4).

        Orders `ORDER BY occurred_at DESC, id DESC`. `cursor` (from a prior
        page's `next_cursor`) is an opaque token decoded server-side into a
        `WHERE (occurred_at, id) < (...)` clause -- avoids `OFFSET`'s O(n)
        scan cost on a table meant to grow unboundedly (insert-only, never
        pruned in this issue's scope). `filters` fields are combined with
        `AND`; every unset (`None`) field is omitted from the `WHERE`
        clause entirely. Filter *validity* (unknown action/resource type,
        `occurred_from > occurred_to`, `page_size` over the maximum) is the
        caller's own
        responsibility, checked before `query` is ever called (AC-BI-008) --
        `query` itself does not re-validate `filters`.

        Raises:
            AuditPostgresUnavailableError: the store cannot be reached
                (AC-BI-011) -- message carries no host/port/driver detail.
            AuditInvalidCursorError: `cursor` does not decode to a
                well-formed `(occurred_at, id)` pair.
        """
        ...


class PsycopgAuditStore:
    """Real `AuditStore` backed by PostgreSQL via `psycopg[binary]`.

    `record` never opens its own connection (uses the caller's cursor), but
    `record_standalone` (Slice 3) and `query` (Slice 4) do, via
    `ps_service.persistence.connect_from_config` -- the *same* PS state
    Postgres instance/config (`config.state_postgres_*`), reused directly
    rather than introducing a parallel `PS_AUDIT_POSTGRES_*` config surface,
    since `audit_events` is created by this component's own migration
    directory in that same database (PLAN.md §3.3).
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Store `config`; no connection is opened (mirrors the other stores' `__init__`)."""
        self._config = config

    def record(
        self,
        cur: psycopg.Cursor[TupleRow],
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Validate `details` against `action`'s registered model, then `INSERT` one row.

        See `AuditStore.record`'s docstring for the full contract. Stored
        `details` omits any field left at its `None` default (e.g.
        a `reason_code` on the `outcome='applied'` path, per the emitting
        component's own field docstrings) rather
        than persisting an explicit `null` -- "absent" and "declared but
        unset" are the same fact for this table's typed, per-action shapes.
        """
        details_model = resolve_details_model(action)
        if details_model is None:
            message = f"audit action {action!r} is not registered with a typed details model"
            raise AuditUnknownActionError(message)
        try:
            validated_details = details_model.model_validate(dict(details))
        except ValidationError as exc:
            message = f"audit action {action!r} details did not match its registered model: {exc}"
            raise AuditInvalidDetailsError(message) from exc
        cur.execute(
            _INSERT_AUDIT_EVENT,
            {
                "actor_subject": actor_subject,
                "actor_issuer": actor_issuer,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "outcome": outcome,
                "details": Json(validated_details.model_dump(mode="json", exclude_none=True)),
            },
        )

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Open its own connection/transaction, call `record`, commit.

        See `AuditStore.record_standalone`'s docstring for the full
        contract. Two distinct failure phases (PLAN.md §3.3): opening the
        connection (`AuditPostgresUnavailableError`, AC-BI-011 -- the
        message never carries `self._config`'s host/port, mirroring
        `AuthorizationStoreUnavailableError`'s own fixed-message discipline)
        versus the `INSERT` itself failing once a connection already exists
        (`AuditPersistenceError`). `AuditUnknownActionError`/
        `AuditInvalidDetailsError` from `record`'s own validation propagate
        unchanged -- they are caller-input errors, not store-availability
        ones, so they must not be reclassified as either audit-store error.
        """
        try:
            conn = connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise AuditPostgresUnavailableError(_AUDIT_STORE_UNAVAILABLE_MESSAGE) from exc
        try:
            with conn, conn.cursor() as cur:
                self.record(
                    cur,
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    outcome=outcome,
                    details=details,
                )
                conn.commit()
        except AuditUnknownActionError, AuditInvalidDetailsError:
            raise
        except psycopg.Error as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            message = f"failed to record standalone audit event {action!r}: {exc}"
            raise AuditPersistenceError(message) from exc
        mark_healthy(STATE_POSTGRES)

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Newest-first, keyset-paginated read of `audit_events`.

        See `AuditStore.query`'s docstring for the full contract. `cursor`
        is decoded (raising `AuditInvalidCursorError`) before any connection
        is opened -- a malformed cursor is a caller-input error, not a
        store-availability one. Fetches `page_size + 1` rows to detect
        whether a next page exists without a separate `COUNT` round trip;
        the (possible) extra row is trimmed before building `next_cursor`
        from the last row actually returned.
        """
        cursor_bound = _decode_cursor(cursor) if cursor is not None else None
        conditions, params = _build_query_conditions(
            filters, cursor_bound=cursor_bound, page_size=page_size
        )

        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        select_sql = (
            f"SELECT {_SELECT_AUDIT_EVENTS_COLUMNS} FROM audit_events{where_clause} "  # noqa: S608 - fixed literal columns/table/conditions, every value parameterized via `params`
            "ORDER BY occurred_at DESC, id DESC LIMIT %(fetch_limit)s"
        )

        try:
            conn = connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise AuditPostgresUnavailableError(_AUDIT_STORE_UNAVAILABLE_MESSAGE) from exc
        try:
            with conn, conn.cursor() as cur:
                # `select_sql` is assembled entirely from this module's own fixed
                # literal fragments (`_SELECT_AUDIT_EVENTS_COLUMNS`, the table name,
                # and a fixed set of possible `WHERE` fragments) -- never from
                # caller-supplied text -- but the assembly is runtime-conditional
                # (which fragments join in depends on which `filters` are set), so
                # it can never statically be a `LiteralString`. `cast()` here mirrors
                # `migration_runner.py`'s own identical `cur.execute(cast(...))`
                # precedent (L2 `cast()` policy). Every actual value is still
                # parameterized via `params`, never interpolated into `select_sql`.
                cur.execute(cast("LiteralString", select_sql), params)
                records = cur.fetchall()
        except psycopg.Error as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise AuditPostgresUnavailableError(_AUDIT_STORE_UNAVAILABLE_MESSAGE) from exc
        mark_healthy(STATE_POSTGRES)

        has_more = len(records) > page_size
        page_records = records[:page_size]
        events = tuple(_row_from_record(record) for record in page_records)
        next_cursor = (
            _encode_cursor(occurred_at=events[-1].occurred_at, event_id=events[-1].id)
            if has_more and events
            else None
        )
        return AuditQueryPage(events=events, next_cursor=next_cursor)


__all__ = ["AuditStore", "PsycopgAuditStore"]
