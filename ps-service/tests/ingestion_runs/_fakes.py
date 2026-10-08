"""Test doubles for `ps_service.ingestion_runs` (issue #194)."""

from __future__ import annotations

import dataclasses
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ps_service.ingestion_runs import (
    IngestionRunPersistenceError,
    IngestionRunRow,
    IngestionRunStoreUnavailableError,
)
from ps_service.ingestion_runs.audit_actions import (
    IngestionRunAuditEntry,
    completion_audit_entry,
    submission_audit_entry,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import psycopg
    from psycopg.rows import TupleRow

    from ps_service.audit import AuditQueryFilters, AuditQueryPage
    from ps_service.ingestion_runs.audit_actions import IngestionReasonCode


class RecordingAuditStore:
    """An `AuditStore`-shaped fake whose `record` only remembers the call (never touches `cur`)."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.recorded: list[dict[str, object]] = []

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
        """Remember the call."""
        del cur
        self.recorded.append(
            {
                "actor_subject": actor_subject,
                "actor_issuer": actor_issuer,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "outcome": outcome,
                "details": dict(details),
            }
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
        """Not used by the ingestion-run store; present for `Protocol` conformance."""
        del actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise NotImplementedError

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not used by the ingestion-run store; present for `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError


@dataclasses.dataclass(frozen=True, slots=True)
class RecordedAudit:
    """One audit entry the fake store recorded, with who it was attributed to and where."""

    actor: tuple[str, str]
    resource_id: str
    entry: IngestionRunAuditEntry
    thread_ident: int


class InMemoryIngestionRunStore:
    """Thread-safe in-memory `IngestionRunStore` with the real store's CAS semantics.

    Knobs: `fail_next_create` / `fail_next_complete` (a count of calls that raise
    `IngestionRunPersistenceError`), `unavailable` (every call raises
    `IngestionRunStoreUnavailableError`) and `raise_on_create` (an arbitrary, non-store
    exception `create_run` raises once, for the unexpected-error paths). `get_run_calls`
    counts `get_run` invocations that reached the store (an unparseable id never does, as in
    the real store).
    """

    def __init__(self) -> None:
        """Start empty with every failure knob off."""
        self._lock = threading.Lock()
        self.rows: dict[str, IngestionRunRow] = {}
        self.fail_next_create = 0
        self.fail_next_complete = 0
        self.unavailable = False
        self.raise_on_create: Exception | None = None
        self.get_run_calls = 0
        self.complete_wins: dict[str, int] = {}
        self.audit_entries: list[RecordedAudit] = []

    def create_run(
        self, *, run_id: str, celex: str, short_name: str, actor: tuple[str, str]
    ) -> IngestionRunRow:
        """Insert one `running` row; see `IngestionRunStore.create_run`."""
        with self._lock:
            if self.unavailable:
                raise IngestionRunStoreUnavailableError
            if self.raise_on_create is not None:
                error, self.raise_on_create = self.raise_on_create, None
                raise error
            if self.fail_next_create > 0:
                self.fail_next_create -= 1
                raise IngestionRunPersistenceError
            row = IngestionRunRow(
                run_id=run_id,
                celex=celex,
                short_name=short_name,
                actor_subject=actor[0],
                actor_issuer=actor[1],
                status="running",
                result=None,
                error=None,
                submitted_at=datetime.now(UTC),
                finished_at=None,
            )
            self.rows[run_id] = row
            self._audit(
                actor,
                run_id,
                submission_audit_entry(celex=celex, short_name=short_name, trigger="async_ingest"),
            )
            return row

    def get_run(self, run_id: str) -> IngestionRunRow | None:
        """Return the row, or `None` for an unknown or non-UUID id."""
        try:
            uuid.UUID(run_id)
        except ValueError:
            return None
        with self._lock:
            self.get_run_calls += 1
            if self.unavailable:
                raise IngestionRunStoreUnavailableError
            return self.rows.get(run_id)

    def complete_run(
        self,
        run_id: str,
        *,
        status: Literal["succeeded", "failed"],
        result: dict[str, object] | None,
        error: str | None,
        reason_code: IngestionReasonCode | None = None,
        audit_actor: tuple[str, str] | None = None,
    ) -> bool:
        """Single-winner compare-and-swap out of `running`; the winner also records its audit."""
        with self._lock:
            if self.unavailable:
                raise IngestionRunStoreUnavailableError
            if self.fail_next_complete > 0:
                self.fail_next_complete -= 1
                raise IngestionRunPersistenceError
            row = self.rows.get(run_id)
            if row is None or row.status != "running":
                return False
            self.rows[run_id] = dataclasses.replace(
                row, status=status, result=result, error=error, finished_at=datetime.now(UTC)
            )
            self.complete_wins[run_id] = self.complete_wins.get(run_id, 0) + 1
            self._audit(
                audit_actor or (row.actor_subject, row.actor_issuer),
                run_id,
                completion_audit_entry(
                    status=status,
                    celex=row.celex,
                    trigger="async_ingest",
                    result=result,
                    reason_code=reason_code,
                ),
            )
            return True

    def _audit(self, actor: tuple[str, str], run_id: str, entry: IngestionRunAuditEntry) -> None:
        self.audit_entries.append(
            RecordedAudit(
                actor=actor,
                resource_id=run_id,
                entry=entry,
                thread_ident=threading.get_ident(),
            )
        )
