"""PostgreSQL store of the insert-only graph mutation log (issue #205).

`GraphLogStore` is the `Protocol` the Graph Write Gateway depends on; `PsycopgGraphLogStore` is
the real implementation, in the idiom of `ps_service.audit.store`: a cursor-scoped write that
joins the caller's transaction (`append_group`), a standalone write that owns its connection
(`append_group_standalone`) and reads that open their own connection.

The store runs as the `ps_state` role, which holds only INSERT and SELECT on the log tables, so
the database itself refuses any rewrite. The store additionally offers no method that could
attempt one.

Sequence allocation: positions are per graph and gap-free. An append takes a transaction-scoped
advisory lock on the graph, reads the graph's highest position and inserts the next ones; the
lock is held until the caller's transaction ends, so a concurrent appender waits, then sees the
committed rows (READ COMMITTED), and a rolled-back group releases its numbers. The database
enforces the same rule independently (see `migrations/0001_graph_mutation_log.sql`).
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Json

from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.errors import (
    GraphLogPayloadError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
)
from ps_service.graph_gateway.models import (
    AppendedGroup,
    AppliedMarker,
    DigestCheckpoint,
    GraphLogEntry,
    GraphLogGroup,
)
from ps_service.graph_gateway.payloads import (
    decode_embedding,
    decode_json_content,
    embedding_payload,
    json_payload_if_large,
)
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg.rows import TupleRow

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft
    from ps_service.graph_gateway.payloads import StoredPayload
    from ps_service.logging import LogEmitter

_COMPONENT = "graph_gateway"
_LOCK_KEY_PREFIX = "ps_graph_log:"
_READ_COMMITTED = "read committed"
_REQUIRES_READ_COMMITTED_MESSAGE = "graph log appends require READ COMMITTED"
_REQUIRES_TRANSACTION_MESSAGE = "graph log appends require an open transaction"
_APPEND_FAILED_MESSAGE = "failed to append the group to the graph log"
_MARKER_AHEAD_MESSAGE = "the applied position may not pass the last logged position"
_MARKER_FAILED_MESSAGE = "failed to advance the applied position"
_CHECKPOINT_FAILED_MESSAGE = "failed to record the digest checkpoint"
_PAYLOAD_REJECTED_MESSAGE = "an entry's content or embedding cannot be stored as a payload"

_LOCK_GRAPH = "SELECT pg_advisory_xact_lock(hashtext(%s))"
_CURRENT_ISOLATION = "SELECT current_setting('transaction_isolation')"
_LAST_POSITION = "SELECT coalesce(max(position), 0) FROM graph_log.entries WHERE graph = %s"
_INSERT_GROUP = """
INSERT INTO graph_log.groups (graph, first_position, last_position, audit_event_id)
VALUES (%(graph)s, %(first_position)s, %(last_position)s, %(audit_event_id)s)
RETURNING group_id
"""
_INSERT_PAYLOAD = """
INSERT INTO graph_log.payloads (payload_hash, kind, byte_length, body)
VALUES (%(payload_hash)s, %(kind)s, %(byte_length)s, %(body)s)
ON CONFLICT (payload_hash) DO NOTHING
"""
_INSERT_ENTRY = """
INSERT INTO graph_log.entries (
    graph, position, group_id, name, identity, content,
    content_payload_hash, embedding_payload_hash
)
VALUES (
    %(graph)s, %(position)s, %(group_id)s, %(name)s, %(identity)s, %(content)s,
    %(content_payload_hash)s, %(embedding_payload_hash)s
)
"""
_ENTRY_SELECT = """
SELECT e.graph, e.position, e.group_id, e.name, e.identity, e.content,
       content_payload.body, embedding_payload.body
FROM graph_log.entries AS e
LEFT JOIN graph_log.payloads AS content_payload
    ON content_payload.payload_hash = e.content_payload_hash
LEFT JOIN graph_log.payloads AS embedding_payload
    ON embedding_payload.payload_hash = e.embedding_payload_hash
"""
_SELECT_APPLIED_POSITION = """
SELECT applied_position FROM graph_log.applied_markers WHERE graph = %s
"""
_ADVANCE_APPLIED_POSITION = """
INSERT INTO graph_log.applied_markers AS marker (graph, applied_position)
VALUES (%(graph)s, %(position)s)
ON CONFLICT (graph) DO UPDATE
SET applied_position = EXCLUDED.applied_position, updated_at = now()
WHERE marker.applied_position < EXCLUDED.applied_position
"""
_INSERT_CHECKPOINT = """
INSERT INTO graph_log.checkpoints (graph, position, canonical_digest)
VALUES (%(graph)s, %(position)s, %(canonical_digest)s)
"""
_SELECT_GRAPHS_WITH_PENDING_ENTRIES = """
SELECT logged.graph
FROM (
    SELECT graph, max(position) AS last_position FROM graph_log.entries GROUP BY graph
) AS logged
LEFT JOIN graph_log.applied_markers AS marker ON marker.graph = logged.graph
WHERE logged.last_position > coalesce(marker.applied_position, 0)
ORDER BY logged.graph
"""
_SELECT_CHECKPOINT = """
SELECT canonical_digest FROM graph_log.checkpoints WHERE graph = %s AND position = %s
"""
_SELECT_GROUPS_BY_AUDIT_EVENT = """
SELECT group_id, graph, first_position, last_position
FROM graph_log.groups
WHERE audit_event_id = %s
ORDER BY graph, first_position
"""
_SELECT_ENTRIES_OF_GROUPS = (
    _ENTRY_SELECT + "WHERE e.group_id = ANY(%s) ORDER BY e.graph, e.position"
)
_SELECT_ENTRIES = _ENTRY_SELECT + "WHERE e.graph = %s AND e.position > %s ORDER BY e.position"


class GraphLogStore(Protocol):
    """Persistence seam for the graph mutation log: append whole groups, read entries back."""

    def append_group(
        self,
        cur: psycopg.Cursor[TupleRow],
        group: GraphLogGroupDraft,
        *,
        audit_event_id: str | None = None,
    ) -> AppendedGroup:
        """Append `group` on the CALLER's open transaction; all of it or none of it.

        `audit_event_id` (the id `AuditStore.record` returned on the same cursor) links the
        group to its audit row by foreign key, so the two commit or roll back together.

        The group's entries take the graph's next contiguous positions. The per-graph advisory
        lock is held until the caller's transaction ends, so callers must make this one of their
        last writes and keep the transaction short. A failure inside rolls back to a savepoint:
        no partial group remains and the caller's earlier writes in the transaction survive.

        Raises:
            GraphLogPersistenceError: the insert or lock failed (including an `audit_event_id`
                that names no audit row), or the caller's transaction is not READ COMMITTED or
                not open.
            ValueError: `audit_event_id` is not a UUID string.
        """
        ...

    def append_group_standalone(
        self, group: GraphLogGroupDraft, *, audit_event_id: str | None = None
    ) -> AppendedGroup:
        """Open a connection, append `group` (linked to `audit_event_id`, if given) and commit.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached (sanitized message).
            GraphLogPersistenceError: the append or the commit failed; nothing was committed.
        """
        ...

    def read_entries(self, graph: str, *, after_position: int = 0) -> tuple[GraphLogEntry, ...]:
        """Return the graph's entries with position above `after_position`, in position order.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
        """
        ...

    def last_position(self, graph: str) -> int:
        """Return the highest recorded position of `graph`, or 0 when it has no entries.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
        """
        ...

    def graphs_with_pending_entries(self) -> tuple[str, ...]:
        """Return, in name order, every graph whose log holds entries beyond its applied marker.

        A graph with no marker counts as applied through position 0. Read-only: it is what
        startup recovery asks to find the graphs it must catch up.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
        """
        ...

    def read_groups_by_audit_event(self, audit_event_id: str) -> tuple[GraphLogGroup, ...]:
        """Return the groups an audit event anchors, each with its entries, ordered by graph.

        One domain command may write several graphs, so an audit event can anchor one group
        per graph; the result is empty when the id anchors nothing.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
            ValueError: `audit_event_id` is not a UUID string.
        """
        ...

    def read_applied_position(self, graph: str) -> int:
        """Return the graph's applied position, or 0 when no marker exists for it.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
        """
        ...

    def advance_applied_position(self, graph: str, position: int) -> AppliedMarker:
        """Advance the graph's applied marker to `position` and return the marker now stored.

        The marker only moves forward: a `position` at or below the stored one changes nothing
        and the stored marker is returned. It never passes the last logged position.

        Raises:
            pydantic.ValidationError: `graph` is blank or `position` is negative.
            GraphLogUnavailableError: `ps_state` could not be reached.
            GraphLogPersistenceError: `position` is beyond the last logged position of `graph`,
                or the write failed; the stored marker is unchanged.
        """
        ...

    def record_digest_checkpoint(
        self, graph: str, position: int, canonical_digest: str
    ) -> DigestCheckpoint:
        """Record `canonical_digest` for `graph` at `position`; a checkpoint is never replaced.

        The digest is opaque here: computing it belongs to the replay component.

        Raises:
            pydantic.ValidationError: `graph` or `canonical_digest` is blank or `position` is
                negative.
            GraphLogUnavailableError: `ps_state` could not be reached.
            GraphLogPersistenceError: a checkpoint already exists at `(graph, position)`, or the
                write failed; the stored checkpoint is unchanged.
        """
        ...

    def read_digest_checkpoint(self, graph: str, position: int) -> DigestCheckpoint | None:
        """Return the checkpoint recorded for `graph` at `position`, or None when there is none.

        Raises:
            GraphLogUnavailableError: `ps_state` could not be reached or the read failed.
        """
        ...


@dataclass(frozen=True)
class _PreparedEntry:
    """An entry draft with its payload rows already built (pure, before any SQL)."""

    draft: GraphLogEntryDraft
    content_payload: StoredPayload | None
    embedding_payload: StoredPayload | None


@dataclass(frozen=True)
class _PayloadWrites:
    """How many payloads a group referenced, and how many of them were already stored."""

    referenced: int
    deduplicated: int


def _prepare_entry(entry: GraphLogEntryDraft) -> _PreparedEntry:
    """Decide where `entry`'s content and embedding live; reject what cannot be stored."""
    try:
        return _PreparedEntry(
            draft=entry,
            content_payload=json_payload_if_large(entry.content),
            embedding_payload=(
                None if entry.embedding is None else embedding_payload(entry.embedding)
            ),
        )
    except ValueError as exc:
        raise GraphLogPayloadError(_PAYLOAD_REJECTED_MESSAGE) from exc


def _entry_from_record(record: tuple[object, ...]) -> GraphLogEntry:
    """Map one `_ENTRY_SELECT` row (fixed column order) to a model.

    `cast()` at this one psycopg boundary mirrors `ps_service.audit.store`: the column types
    are fixed by `migrations/0001_graph_mutation_log.sql`.
    """
    graph, position, group_id, name, identity, inline_content, content_body, embedding_body = record
    content = (
        cast("dict[str, object]", inline_content)
        if content_body is None
        else decode_json_content(cast("bytes", content_body))
    )
    return GraphLogEntry(
        graph=cast("str", graph),
        position=cast("int", position),
        group_id=cast("uuid.UUID", group_id),
        name=cast("str", name),
        identity=cast("str", identity),
        content=content,
        embedding=(
            None if embedding_body is None else decode_embedding(cast("bytes", embedding_body))
        ),
    )


def _stored_applied_position(cur: psycopg.Cursor[TupleRow], graph: str) -> int:
    """Read the graph's stored applied position on `cur`; 0 when it has no marker."""
    cur.execute(_SELECT_APPLIED_POSITION, (graph,))
    row = cur.fetchone()
    return 0 if row is None else cast("int", row[0])


def _hash_of(payload: StoredPayload | None) -> str | None:
    """Return the payload's content address, or None when the value is stored inline."""
    return None if payload is None else payload.payload_hash


def _parse_audit_event_id(audit_event_id: str | None) -> uuid.UUID | None:
    """Parse the optional audit event id once at the boundary (`ValueError` if malformed)."""
    return None if audit_event_id is None else uuid.UUID(audit_event_id)


def _error_class_name(exc: Exception) -> str:
    """Name the underlying driver error class if there is one, else the error's own class."""
    return type(exc.__cause__ if exc.__cause__ is not None else exc).__name__


class PsycopgGraphLogStore:
    """Real `GraphLogStore` backed by the PS state Postgres via `psycopg[binary]`."""

    def __init__(self, config: ServiceConfig, *, emitter: LogEmitter | None = None) -> None:
        """Store `config` (and an optional log emitter); no connection is opened."""
        self._config = config
        self._emitter = emitter

    def append_group(
        self,
        cur: psycopg.Cursor[TupleRow],
        group: GraphLogGroupDraft,
        *,
        audit_event_id: str | None = None,
    ) -> AppendedGroup:
        """Append `group` on the caller's transaction (see `GraphLogStore.append_group`)."""
        linked_event = _parse_audit_event_id(audit_event_id)
        try:
            appended, writes = self._append_in_transaction(cur, group, linked_event)
        except GraphLogPersistenceError as exc:
            self._emit_append(group, linked_event, outcome="failure", error=exc)
            raise
        self._emit_append(group, linked_event, outcome="success", appended=appended, writes=writes)
        return appended

    def append_group_standalone(
        self, group: GraphLogGroupDraft, *, audit_event_id: str | None = None
    ) -> AppendedGroup:
        """Append `group` in its own transaction (see `GraphLogStore.append_group_standalone`)."""
        linked_event = _parse_audit_event_id(audit_event_id)
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                appended, writes = self._append_in_transaction(cur, group, linked_event)
        except GraphLogPersistenceError as exc:
            self._emit_append(group, linked_event, outcome="failure", error=exc)
            raise
        except psycopg.Error as exc:  # raised by the commit itself (deferred database checks)
            failure = GraphLogPersistenceError(_APPEND_FAILED_MESSAGE)
            failure.__cause__ = exc
            self._emit_append(group, linked_event, outcome="failure", error=failure)
            raise failure from exc
        mark_healthy(STATE_POSTGRES)
        self._emit_append(group, linked_event, outcome="success", appended=appended, writes=writes)
        return appended

    def read_entries(self, graph: str, *, after_position: int = 0) -> tuple[GraphLogEntry, ...]:
        """Read the graph's entries after `after_position` (see `GraphLogStore.read_entries`)."""

        def select_entries(cur: psycopg.Cursor[TupleRow]) -> tuple[GraphLogEntry, ...]:
            cur.execute(_SELECT_ENTRIES, (graph, after_position))
            return tuple(_entry_from_record(record) for record in cur.fetchall())

        return self._read(select_entries)

    def last_position(self, graph: str) -> int:
        """Read the graph's highest position (see `GraphLogStore.last_position`)."""

        def select_last_position(cur: psycopg.Cursor[TupleRow]) -> int:
            cur.execute(_LAST_POSITION, (graph,))
            return cast("int", cast("TupleRow", cur.fetchone())[0])

        return self._read(select_last_position)

    def graphs_with_pending_entries(self) -> tuple[str, ...]:
        """Read the graphs behind their log (see `GraphLogStore.graphs_with_pending_entries`)."""

        def select_graphs(cur: psycopg.Cursor[TupleRow]) -> tuple[str, ...]:
            cur.execute(_SELECT_GRAPHS_WITH_PENDING_ENTRIES)
            return tuple(cast("str", record[0]) for record in cur.fetchall())

        return self._read(select_graphs)

    def read_groups_by_audit_event(self, audit_event_id: str) -> tuple[GraphLogGroup, ...]:
        """Read the groups an audit event anchors (see the `GraphLogStore` method)."""
        linked_event = uuid.UUID(audit_event_id)

        def select_groups(cur: psycopg.Cursor[TupleRow]) -> tuple[GraphLogGroup, ...]:
            cur.execute(_SELECT_GROUPS_BY_AUDIT_EVENT, (linked_event,))
            group_records = cur.fetchall()
            group_ids = [cast("uuid.UUID", record[0]) for record in group_records]
            cur.execute(_SELECT_ENTRIES_OF_GROUPS, (group_ids,))
            entries_by_group: dict[uuid.UUID, list[GraphLogEntry]] = {}
            for record in cur.fetchall():
                entry = _entry_from_record(record)
                entries_by_group.setdefault(entry.group_id, []).append(entry)
            return tuple(
                GraphLogGroup(
                    group_id=cast("uuid.UUID", group_id),
                    graph=cast("str", graph),
                    first_position=cast("int", first_position),
                    last_position=cast("int", last_position),
                    audit_event_id=audit_event_id,
                    entries=tuple(entries_by_group.get(cast("uuid.UUID", group_id), ())),
                )
                for group_id, graph, first_position, last_position in group_records
            )

        return self._read(select_groups)

    def read_applied_position(self, graph: str) -> int:
        """Read the graph's applied position (see `GraphLogStore.read_applied_position`)."""

        def select_applied_position(cur: psycopg.Cursor[TupleRow]) -> int:
            return _stored_applied_position(cur, graph)

        return self._read(select_applied_position)

    def advance_applied_position(self, graph: str, position: int) -> AppliedMarker:
        """Advance the applied marker, never backwards (see `GraphLogStore`)."""
        target = AppliedMarker(graph=graph, applied_position=position)

        def advance(cur: psycopg.Cursor[TupleRow]) -> AppliedMarker:
            cur.execute(_LAST_POSITION, (graph,))
            if position > cast("int", cast("TupleRow", cur.fetchone())[0]):
                raise GraphLogPersistenceError(_MARKER_AHEAD_MESSAGE)
            cur.execute(_ADVANCE_APPLIED_POSITION, {"graph": graph, "position": position})
            return AppliedMarker(graph=graph, applied_position=_stored_applied_position(cur, graph))

        return self._write_logged(
            "advance_applied_position",
            {"graph": target.graph, "position": target.applied_position},
            advance,
            _MARKER_FAILED_MESSAGE,
        )

    def record_digest_checkpoint(
        self, graph: str, position: int, canonical_digest: str
    ) -> DigestCheckpoint:
        """Record a digest checkpoint, never replacing one (see `GraphLogStore`)."""
        checkpoint = DigestCheckpoint(
            graph=graph, position=position, canonical_digest=canonical_digest
        )

        def insert_checkpoint(cur: psycopg.Cursor[TupleRow]) -> DigestCheckpoint:
            cur.execute(_INSERT_CHECKPOINT, checkpoint.model_dump())
            return checkpoint

        return self._write_logged(
            "record_digest_checkpoint",
            {"graph": checkpoint.graph, "position": checkpoint.position},
            insert_checkpoint,
            _CHECKPOINT_FAILED_MESSAGE,
        )

    def read_digest_checkpoint(self, graph: str, position: int) -> DigestCheckpoint | None:
        """Read the checkpoint at `(graph, position)` (see `GraphLogStore`)."""

        def select_checkpoint(cur: psycopg.Cursor[TupleRow]) -> DigestCheckpoint | None:
            cur.execute(_SELECT_CHECKPOINT, (graph, position))
            row = cur.fetchone()
            if row is None:
                return None
            return DigestCheckpoint(
                graph=graph, position=position, canonical_digest=cast("str", row[0])
            )

        return self._read(select_checkpoint)

    def _connect(self) -> psycopg.Connection[TupleRow]:
        """Open a connection, mapping any failure to the sanitized unavailable error."""
        try:
            return connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise GraphLogUnavailableError from exc

    def _read[ReadResult](
        self, select: Callable[[psycopg.Cursor[TupleRow]], ReadResult]
    ) -> ReadResult:
        """Run `select` on its own connection, mapping driver failures to the unavailable error."""
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                found = select(cur)
        except psycopg.Error as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise GraphLogUnavailableError from exc
        mark_healthy(STATE_POSTGRES)
        return found

    def _write_logged[WriteResult](
        self,
        action: str,
        extra: dict[str, object],
        work: Callable[[psycopg.Cursor[TupleRow]], WriteResult],
        failure_message: str,
    ) -> WriteResult:
        """Run `work` in its own transaction and log one `action` entry for its outcome."""
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                done = work(cur)
        except GraphLogPersistenceError as exc:
            self._emit_operation(action, "failure", extra, error=exc)
            raise
        except psycopg.Error as exc:
            failure = GraphLogPersistenceError(failure_message)
            failure.__cause__ = exc
            self._emit_operation(action, "failure", extra, error=failure)
            raise failure from exc
        mark_healthy(STATE_POSTGRES)
        self._emit_operation(action, "success", extra)
        return done

    def _append_in_transaction(
        self,
        cur: psycopg.Cursor[TupleRow],
        group: GraphLogGroupDraft,
        audit_event_id: uuid.UUID | None,
    ) -> tuple[AppendedGroup, _PayloadWrites]:
        """Build the payloads, take the graph lock, then insert everything under a savepoint."""
        prepared = tuple(_prepare_entry(entry) for entry in group.entries)
        try:
            self._lock_graph_in_read_committed_transaction(cur, group.graph)
            with cur.connection.transaction():
                return self._insert_group(cur, group.graph, prepared, audit_event_id)
        except psycopg.Error as exc:
            raise GraphLogPersistenceError(_APPEND_FAILED_MESSAGE) from exc

    def _lock_graph_in_read_committed_transaction(
        self, cur: psycopg.Cursor[TupleRow], graph: str
    ) -> None:
        """Serialize appenders of `graph` until the caller's transaction ends, and check it."""
        cur.execute(_LOCK_GRAPH, (f"{_LOCK_KEY_PREFIX}{graph}",))
        if cur.connection.info.transaction_status != TransactionStatus.INTRANS:
            raise GraphLogPersistenceError(_REQUIRES_TRANSACTION_MESSAGE)
        cur.execute(_CURRENT_ISOLATION)
        isolation = cur.fetchone()
        if isolation is None or isolation[0] != _READ_COMMITTED:
            raise GraphLogPersistenceError(_REQUIRES_READ_COMMITTED_MESSAGE)

    def _insert_group(
        self,
        cur: psycopg.Cursor[TupleRow],
        graph: str,
        entries: tuple[_PreparedEntry, ...],
        audit_event_id: uuid.UUID | None,
    ) -> tuple[AppendedGroup, _PayloadWrites]:
        """Insert the group row, then its entries at the graph's next contiguous positions."""
        cur.execute(_LAST_POSITION, (graph,))
        last_logged = cast("int", cast("TupleRow", cur.fetchone())[0])
        first_position = last_logged + 1
        last_position = last_logged + len(entries)
        cur.execute(
            _INSERT_GROUP,
            {
                "graph": graph,
                "first_position": first_position,
                "last_position": last_position,
                "audit_event_id": audit_event_id,
            },
        )
        group_id = cast("uuid.UUID", cast("TupleRow", cur.fetchone())[0])
        newly_stored: list[bool] = []
        for offset, entry in enumerate(entries):
            newly_stored.extend(
                self._insert_entry(cur, graph, first_position + offset, group_id, entry)
            )
        appended = AppendedGroup(
            group_id=group_id,
            graph=graph,
            first_position=first_position,
            last_position=last_position,
        )
        writes = _PayloadWrites(
            referenced=len(newly_stored), deduplicated=newly_stored.count(False)
        )
        return appended, writes

    def _insert_entry(
        self,
        cur: psycopg.Cursor[TupleRow],
        graph: str,
        position: int,
        group_id: uuid.UUID,
        entry: _PreparedEntry,
    ) -> list[bool]:
        """Insert one entry after its payloads; one flag per payload, True when newly stored."""
        payloads = [p for p in (entry.content_payload, entry.embedding_payload) if p is not None]
        newly_stored = [self._insert_payload(cur, payload) for payload in payloads]
        cur.execute(
            _INSERT_ENTRY,
            {
                "graph": graph,
                "position": position,
                "group_id": group_id,
                "name": entry.draft.name,
                "identity": entry.draft.identity,
                "content": Json(entry.draft.content) if entry.content_payload is None else None,
                "content_payload_hash": _hash_of(entry.content_payload),
                "embedding_payload_hash": _hash_of(entry.embedding_payload),
            },
        )
        return newly_stored

    def _insert_payload(self, cur: psycopg.Cursor[TupleRow], payload: StoredPayload) -> bool:
        """Insert `payload` unless its hash is already stored; True when a row was added."""
        cur.execute(
            _INSERT_PAYLOAD,
            {
                "payload_hash": payload.payload_hash,
                "kind": payload.kind,
                "byte_length": len(payload.body),
                "body": payload.body,
            },
        )
        return cur.rowcount == 1

    def _emit_append(
        self,
        group: GraphLogGroupDraft,
        audit_event_id: uuid.UUID | None,
        *,
        outcome: str,
        appended: AppendedGroup | None = None,
        writes: _PayloadWrites | None = None,
        error: Exception | None = None,
    ) -> None:
        """Log one `append_group` entry: counts, positions and ids, never entry content."""
        extra: dict[str, object] = {"graph": group.graph, "entry_count": len(group.entries)}
        if audit_event_id is not None:
            extra["audit_event_id"] = str(audit_event_id)
        if appended is not None:
            extra["group_id"] = str(appended.group_id)
            extra["first_position"] = appended.first_position
            extra["last_position"] = appended.last_position
        if writes is not None:
            extra["payload_count"] = writes.referenced
            extra["deduplicated_count"] = writes.deduplicated
        self._emit_operation("append_group", outcome, extra, error=error)

    def _emit_operation(
        self,
        action: str,
        outcome: str,
        extra: dict[str, object],
        *,
        error: Exception | None = None,
    ) -> None:
        """Log one semantic entry of this component; a failure adds only the error class."""
        if error is not None:
            extra = {**extra, "error_class": _error_class_name(error)}
        with contextlib.suppress(LoggingLifecycleError):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome=outcome,
                extra=extra,
                emitter=self._emitter,
            )


__all__ = ["GraphLogStore", "PsycopgGraphLogStore"]
