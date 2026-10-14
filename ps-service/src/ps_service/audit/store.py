"""PostgreSQL persistence for the shared, insert-only `audit_events` table.

`AuditStore` is the `Protocol` that every `ps_service` component emitting audit events depends
on; `PsycopgAuditStore` is the real implementation. The store is not nested under any one
consumer component, even though `audit_events` lives in the PS state Postgres instance that
several components share. It opens connections with `ps_service.persistence.connect_from_config`
rather than adding a parallel configuration surface; the table is created by this component's
own migration directory, applied by the shared `ps_service.persistence` runner.

Three operations:

- `record`: the cursor-scoped write for callers that already hold a transaction, so the audit row
  commits or rolls back with their state change.
- `record_standalone`: opens its own connection and commits, for rows with no surrounding
  state-changing transaction (denials, opening and terminal lifecycle rows).
- `query`: newest-first, keyset-paginated, filtered read.

Insert-only is enforced by omission, not by a database privilege or trigger: `AuditStore` defines
no `UPDATE` or `DELETE` method at all.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

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
    from typing import Literal, LiteralString  # noqa: V104 - named only inside a string cast

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
RETURNING id
"""

_SELECT_AUDIT_EVENTS_COLUMNS = (
    "id, occurred_at, actor_subject, actor_issuer, action, resource_type, resource_id, "
    "outcome, details"
)


def _encode_cursor(*, occurred_at: datetime, event_id: str) -> str:
    """Encode a keyset position as the opaque `next_cursor` (base64 of `"{occurred_at_iso}|{id}"`).

    Callers must treat the token as opaque: it only round-trips through `query`'s `cursor`.
    """
    raw = f"{occurred_at.isoformat()}{_CURSOR_FIELD_SEPARATOR}{event_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a `cursor` into its `(occurred_at, id)` keyset position.

    Raises `AuditInvalidCursorError` for any malformed shape: not valid base64, the wrong field
    count once decoded, an unparseable timestamp, or a non-UUID id.
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


_DETAILS_FILTER_FRAGMENTS: dict[str, str] = {
    "celex": "(details ->> 'celex') = %(details_celex)s",
    "regulatory_instrument_id": (
        "(details ->> 'regulatory_instrument_id') = %(details_regulatory_instrument_id)s"
    ),
    "instrument_id": "(details ->> 'instrument_id') = %(details_instrument_id)s",
}
"""Fixed SQL fragment per `AUDIT_DETAILS_FILTER_KEYS` member (expression text matches the
`migrations/0002` indexes). The caller's key string is only ever used to *select* a fragment
from this dict, never interpolated into SQL; the value is bound as `details_<key>`."""


def _build_query_conditions(
    filters: AuditQueryFilters, *, cursor_bound: tuple[datetime, str] | None, page_size: int
) -> tuple[list[str], dict[str, object]]:
    """Build `query`'s `WHERE` fragments and their bound parameter values.

    Every fragment is a fixed literal; only which fragments are included depends on the set
    filters. Every filter value travels in the returned `params` dict, never interpolated into a
    fragment. A `filters.details` key outside `AUDIT_DETAILS_FILTER_KEYS` raises `KeyError`.
    """
    conditions: list[str] = []
    params: dict[str, object] = {"fetch_limit": page_size + 1}
    for field_name, column in _EQUALITY_FILTER_COLUMNS:
        value = getattr(filters, field_name)
        if value is not None:
            conditions.append(f"{column} = %({field_name})s")
            params[field_name] = value
    for key, value in (filters.details or {}).items():
        conditions.append(_DETAILS_FILTER_FRAGMENTS[key])
        params[f"details_{key}"] = value
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
    """Map one raw `psycopg` result row (column order `_SELECT_AUDIT_EVENTS_COLUMNS`) to a row.

    Column types are fixed by `migrations/0001_audit_events.sql`, so `cast()` at this one
    boundary is safe. `id` is converted with `str()` because `psycopg` returns a `uuid.UUID`.
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
    """Persistence seam for `audit_events` rows: validated insert and filtered, paginated read.

    Constructor-injected into every component that records or reads audit events. Consumers
    depend on this `Protocol`, never on `PsycopgAuditStore` directly, so a test can substitute
    an in-memory fake. The interface has no update or delete method: the trail is insert-only.
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
    ) -> str:
        """Validate `details` against `action`'s registered model, then insert one audit row.

        The insert runs on the caller's own open cursor and transaction, never on a connection
        this method opens, so it commits or rolls back atomically with whatever state change that
        transaction also performs. The caller owns the commit.

        Args:
            cur: The caller's open cursor, inside the transaction the row must join.
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload. Fields left at their `None` default are
                omitted from the stored JSON rather than stored as `null`.

        Returns:
            The generated id (a UUID string) of the new row, so the caller can link other writes
            in the same transaction to this audit row.

        Raises:
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails that model's validation; nothing is
                inserted.
            AuditPersistenceError: the insert returned no row.
            psycopg.Error: the driver failed; propagated unchanged for the caller's own
                transaction handling, since this method owns no transaction to roll back.

        Side Effects:
            Executes one `INSERT` on `cur`; becomes durable only when the caller commits.
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
        """Validate and insert one audit row in its own connection and transaction, then commit.

        For rows with no surrounding state-changing transaction to join, such as a denial
        recorded before any state change was attempted, or an opening or terminal lifecycle
        row.

        Args:
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload, validated against the action's model.

        Returns:
            None.

        Raises:
            AuditPostgresUnavailableError: the connection could not be opened; the message
                carries no host, port or driver detail.
            AuditPersistenceError: the connection opened but the insert failed.
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails the action's model; nothing is inserted.

        Side Effects:
            Opens and closes a connection to the PS state Postgres, inserts and commits one
            `audit_events` row, and updates the state-Postgres dependency health (unhealthy on a
            connection or driver failure, healthy on success).
        """
        ...

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Read one newest-first, keyset-paginated page of `audit_events`.

        Rows are ordered by `occurred_at DESC, id DESC`. Keyset pagination (rather than `OFFSET`)
        keeps page cost flat on a table that only grows.

        Business Rules:
            Set filter fields are combined with AND; an unset field is omitted. The store does
            not validate filter values (unknown action or resource type, `occurred_from` after
            `occurred_to`, an oversized `page_size`): the caller must check them first.

        Args:
            filters: The filters to apply.
            cursor: The `next_cursor` of a previous page, or `None` for the first page.
            page_size: Maximum number of rows to return; a positive integer.

        Returns:
            An `AuditQueryPage` holding up to `page_size` rows and a `next_cursor` that is `None`
            when no further rows exist.

        Raises:
            AuditInvalidCursorError: `cursor` does not decode to a well-formed
                `(occurred_at, id)` pair.
            AuditPostgresUnavailableError: the store cannot be reached or the read fails; the
                message carries no host, port or driver detail.

        Side Effects:
            Opens and closes a connection to the PS state Postgres and updates the
            state-Postgres dependency health. Writes nothing.
        """
        ...


@runtime_checkable
class EventIdAuditStore(Protocol):
    """An audit store that can report the id of the row it just wrote.

    Narrow capability on top of `AuditStore`, requested only by a caller that must anchor a
    later write to the audit row that opened its operation. `PsycopgAuditStore` satisfies it.
    The check is runtime-checkable (`isinstance`).
    """

    def record_standalone_with_event_id(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> str:
        """Validate and insert one audit row in its own connection and transaction, return its id.

        Same contract as `AuditStore.record_standalone`, but hands back the generated id.

        Args:
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload, validated against the action's model.

        Returns:
            The generated id (a UUID string) of the inserted `audit_events` row.

        Raises:
            AuditPostgresUnavailableError: the connection could not be opened; the message
                carries no host, port or driver detail.
            AuditPersistenceError: the connection opened but the insert failed.
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails the action's model; nothing is inserted.

        Side Effects:
            Opens and closes a connection to the PS state Postgres, inserts and commits one
            `audit_events` row, and updates the state-Postgres dependency health.
        """
        ...


class PsycopgAuditStore:
    """`AuditStore` and `EventIdAuditStore` backed by PostgreSQL via `psycopg`.

    `record` never opens a connection (it uses the caller's cursor). `record_standalone`,
    `record_standalone_with_event_id` and `query` open one with
    `ps_service.persistence.connect_from_config`, using the same PS state Postgres configuration
    (`config.state_postgres_*`) that holds `audit_events`; there is no separate audit database
    setting.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Keep `config` for later connections; opens none.

        Args:
            config: Service configuration whose state-Postgres settings locate `audit_events`.
        """
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
    ) -> str:
        """Validate `details` against `action`'s registered model, then insert one audit row.

        The insert runs on the caller's own open cursor and transaction, so it commits or rolls
        back with the caller's state change. Stored `details` omits any field left at its `None`
        default instead of persisting an explicit `null`: "absent" and "declared but unset" are
        the same fact for the table's typed per-action shapes.

        Args:
            cur: The caller's open cursor, inside the transaction the row must join.
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload; validated, then dumped as JSON.

        Returns:
            The generated id (a UUID string) of the new row.

        Raises:
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails that model's validation; nothing is
                inserted. The message includes the validation error text.
            AuditPersistenceError: the insert returned no row.
            psycopg.Error: the driver failed; propagated unchanged for the caller's transaction
                handling.

        Side Effects:
            Executes one `INSERT` on `cur`; becomes durable only when the caller commits.
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
        inserted = cur.fetchone()
        if inserted is None:  # pragma: no cover - INSERT ... RETURNING always yields its row
            message = f"audit insert for action {action!r} returned no row"
            raise AuditPersistenceError(message)
        return str(inserted[0])

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
        """Validate and insert one audit row in its own connection and transaction, then commit.

        Behaves as `record_standalone_with_event_id` and discards the returned id.

        Args:
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload, validated against the action's model.

        Returns:
            None.

        Raises:
            AuditPostgresUnavailableError: the connection could not be opened; the message
                carries no host, port or driver detail.
            AuditPersistenceError: the connection opened but the insert failed.
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails the action's model; nothing is inserted.

        Side Effects:
            Opens and closes a connection to the PS state Postgres, inserts and commits one
            `audit_events` row, and updates the state-Postgres dependency health.
        """
        self.record_standalone_with_event_id(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )

    def record_standalone_with_event_id(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> str:
        """Validate and insert one audit row in its own connection and transaction, return its id.

        Two failure phases are kept distinct: opening the connection raises
        `AuditPostgresUnavailableError` (fixed message, no host or port), while an `INSERT`
        failure on an open connection raises `AuditPersistenceError`. Validation errors are
        caller-input errors, not store-availability ones, so they propagate unchanged and are
        never reclassified as either store error.

        Args:
            actor_subject: Identity-provider subject of the acting principal.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            action: A registered, namespaced action string.
            resource_type: The kind of resource acted on.
            resource_id: The identifier of the resource acted on.
            outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
            details: The action's `details` payload, validated against the action's model.

        Returns:
            The generated id (a UUID string) of the inserted `audit_events` row.

        Raises:
            AuditPostgresUnavailableError: the connection could not be opened (state Postgres
                unconfigured or unreachable).
            AuditPersistenceError: the connection opened but the insert failed; wraps the driver
                error.
            AuditUnknownActionError: `action` has no registered model; nothing is inserted.
            AuditInvalidDetailsError: `details` fails the action's model; nothing is inserted.

        Side Effects:
            Opens and closes a connection to the PS state Postgres, inserts and commits one
            `audit_events` row, marks the state-Postgres dependency unhealthy on a connection or
            driver failure and healthy on success.
        """
        try:
            conn = connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise AuditPostgresUnavailableError(_AUDIT_STORE_UNAVAILABLE_MESSAGE) from exc
        try:
            with conn, conn.cursor() as cur:
                event_id = self.record(
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
        return event_id

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Read one newest-first, keyset-paginated page of `audit_events`.

        Rows are ordered by `occurred_at DESC, id DESC`. The cursor is decoded before any
        connection is opened, because a malformed cursor is a caller-input error, not a
        store-availability one. `page_size + 1` rows are fetched to learn whether a next page
        exists without a `COUNT` round trip; the extra row is dropped and `next_cursor` is built
        from the last row returned.

        Args:
            filters: Filters combined with AND; unset fields are omitted. Values are not
                validated here.
            cursor: The `next_cursor` of a previous page, or `None` for the first page.
            page_size: Maximum number of rows to return; a positive integer.

        Returns:
            An `AuditQueryPage` holding up to `page_size` rows, newest first (empty when nothing
            matches), and a `next_cursor` that is `None` on the last page.

        Raises:
            AuditInvalidCursorError: `cursor` is malformed.
            AuditPostgresUnavailableError: the connection could not be opened or the read failed
                with a driver error; the message carries no host, port or driver detail.
            KeyError: `filters.details` contains a key outside `AUDIT_DETAILS_FILTER_KEYS`.

        Side Effects:
            Opens and closes a connection to the PS state Postgres and marks the state-Postgres
            dependency unhealthy on failure and healthy on success. Writes nothing.
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
                # `select_sql` is assembled only from this module's fixed literal fragments,
                # never from caller text, but which fragments join depends on the set filters,
                # so the type checker cannot see it as a `LiteralString`. Every value is
                # still bound through `params`, never interpolated.
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
