"""PostgreSQL persistence for `ingestion_runs` (issue #194).

`IngestionRunStore` is the `Protocol` the MCP tools depend on; `PsycopgIngestionRunStore` is
the real implementation, modeled on `passkey_signing`'s pending-approval store (a `Protocol`,
a per-call connection, a single-winner compare-and-swap) and on `runtime_config`'s failure
discipline: connection or read failures raise `IngestionRunStoreUnavailableError`, write
failures `IngestionRunPersistenceError`; both carry fixed messages that never contain host,
port or driver text. Log entries carry the run id and the exception class name only.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Literal, Protocol, cast

import psycopg
from psycopg.types.json import Json

from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.ingestion_runs.audit_actions import (
    INGESTION_RUN_RESOURCE_TYPE,
    completion_audit_entry,
    submission_audit_entry,
)
from ps_service.ingestion_runs.errors import (
    IngestionRunPersistenceError,
    IngestionRunStoreUnavailableError,
)
from ps_service.ingestion_runs.models import IngestionRunRow
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError, connect_from_config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from psycopg.rows import TupleRow

    from ps_service.audit import AuditStore
    from ps_service.config import ServiceConfig
    from ps_service.ingestion_runs.audit_actions import IngestionReasonCode, IngestionTrigger
    from ps_service.logging import LogEmitter

_COMPONENT = "ingestion_runs"
_TRIGGER: IngestionTrigger = "async_ingest"
"""Every row of this store is a `start_ingestion` run; sync and sweep runs have no run row."""

_COLUMNS = (
    "run_id, celex, short_name, actor_subject, actor_issuer, status, result, error, "
    "submitted_at, finished_at"
)
_INSERT_RUN = f"""
INSERT INTO ingestion_runs (run_id, celex, short_name, actor_subject, actor_issuer)
VALUES (%(run_id)s, %(celex)s, %(short_name)s, %(actor_subject)s, %(actor_issuer)s)
RETURNING {_COLUMNS}
"""  # noqa: S608 - fixed column list, no interpolated user input
_SELECT_RUN = f"SELECT {_COLUMNS} FROM ingestion_runs WHERE run_id = %(run_id)s"  # noqa: S608 - fixed literal, no interpolated user input
_COMPLETE_RUN = """
UPDATE ingestion_runs
SET status = %(status)s, result = %(result)s, error = %(error)s, finished_at = now()
WHERE run_id = %(run_id)s AND status = 'running'
RETURNING actor_subject, actor_issuer, celex
"""


class IngestionRunStore(Protocol):
    """Persistence seam for ingestion runs, constructor-injected wherever needed."""

    def create_run(
        self, *, run_id: str, celex: str, short_name: str, actor: tuple[str, str]
    ) -> IngestionRunRow:
        """Insert one row with status `running` and return it.

        Raises:
            IngestionRunStoreUnavailableError: the store could not be reached.
            IngestionRunPersistenceError: the insert failed; nothing was written.
        """
        ...

    def get_run(self, run_id: str) -> IngestionRunRow | None:
        """Return the row, or `None` for an unknown id and for any text that is not a UUID.

        Raises:
            IngestionRunStoreUnavailableError: the store could not be reached or read.
        """
        ...

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
        """Flip a `running` row to a terminal status: a single-winner compare-and-swap.

        Returns `True` only for the one call that flipped the row, and only that call writes the
        `ingestion_run.complete` audit row, in the same transaction. `audit_actor=None` audits
        it under the submitter (the row's own actor); the reconciler passes its sentinel.
        `error` is the sanitized text kept on the run row for `get_ingestion_status`; the audit
        row never carries it, only the enumerated `reason_code` (required when `status='failed'`).

        Raises:
            IngestionRunStoreUnavailableError: the store could not be reached.
            IngestionRunPersistenceError: the update failed; nothing was written.
        """
        ...


def _row_from_record(record: Sequence[object]) -> IngestionRunRow:
    """Map one raw result row (fixed column order, see `_COLUMNS`) to an `IngestionRunRow`.

    `cast()` is unavoidable at exactly this one boundary (L2's cast() policy): `psycopg`'s
    tuple-row result carries no per-column static type. Every column's Python type is fixed
    by `migrations/0001_ingestion_runs.sql`.
    """
    (
        run_id,
        celex,
        short_name,
        actor_subject,
        actor_issuer,
        status,
        result,
        error,
        submitted_at,
        finished_at,
    ) = record
    return IngestionRunRow(
        run_id=str(run_id),
        celex=cast("str", celex),
        short_name=cast("str", short_name),
        actor_subject=cast("str", actor_subject),
        actor_issuer=cast("str", actor_issuer),
        status=cast('Literal["running", "succeeded", "failed"]', status),
        result=cast("dict[str, object] | None", result),
        error=cast("str | None", error),
        submitted_at=cast("datetime", submitted_at),
        finished_at=cast("datetime | None", finished_at),
    )


class PsycopgIngestionRunStore:
    """Real `IngestionRunStore` backed by PostgreSQL via `psycopg[binary]`.

    Every method opens, uses and closes its own connection (`connect_from_config`): no pool,
    and no connection held across calls, so the background worker's terminal write runs on
    its own fresh connection, independent of the call that submitted the run.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        audit_store: AuditStore,
        emitter: LogEmitter | None = None,
    ) -> None:
        """Store `config`, the audit store and an optional emitter; connect nothing."""
        self._config = config
        self._audit_store = audit_store
        self._emitter = emitter

    def create_run(
        self, *, run_id: str, celex: str, short_name: str, actor: tuple[str, str]
    ) -> IngestionRunRow:
        """Insert one `running` row; see `IngestionRunStore.create_run`."""
        params = {
            "run_id": run_id,
            "celex": celex,
            "short_name": short_name,
            "actor_subject": actor[0],
            "actor_issuer": actor[1],
        }
        try:
            conn = self._connect()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(_INSERT_RUN, params)
                    record = cur.fetchone()
                    entry = submission_audit_entry(
                        celex=celex, short_name=short_name, trigger=_TRIGGER
                    )
                    self._audit_store.record(
                        cur,
                        actor_subject=actor[0],
                        actor_issuer=actor[1],
                        action=entry.action,
                        resource_type=INGESTION_RUN_RESOURCE_TYPE,
                        resource_id=run_id,
                        outcome=entry.outcome,
                        details=entry.details,
                    )
                    conn.commit()
            except psycopg.Error as exc:
                mark_unhealthy(STATE_POSTGRES, error=exc)
                raise IngestionRunPersistenceError from exc
        except (IngestionRunStoreUnavailableError, IngestionRunPersistenceError) as exc:
            self._log("create_run", "failed", run_id, exc.__cause__ or exc)
            raise
        mark_healthy(STATE_POSTGRES)
        if record is None:  # an INSERT ... RETURNING always yields its row
            raise IngestionRunPersistenceError
        self._log("create_run", "success", run_id, None)
        return _row_from_record(record)

    def get_run(self, run_id: str) -> IngestionRunRow | None:
        """Read one row by primary key; see `IngestionRunStore.get_run`."""
        try:
            uuid.UUID(run_id)
        except ValueError:
            return None  # a `uuid` column comparison against non-UUID text raises in Postgres
        try:
            conn = self._connect()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(_SELECT_RUN, {"run_id": run_id})
                    record = cur.fetchone()
            except psycopg.Error as exc:
                mark_unhealthy(STATE_POSTGRES, error=exc)
                raise IngestionRunStoreUnavailableError from exc
        except IngestionRunStoreUnavailableError as exc:
            self._log("get_run", "failed", run_id, exc.__cause__ or exc)
            raise
        mark_healthy(STATE_POSTGRES)
        return None if record is None else _row_from_record(record)

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
        """Compare-and-swap a `running` row to a terminal status; see the Protocol."""
        params = {
            "run_id": run_id,
            "status": status,
            "result": None if result is None else Json(result),
            "error": error,
        }
        try:
            conn = self._connect()
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(_COMPLETE_RUN, params)
                    flipped = cur.fetchone()
                    won = flipped is not None
                    if flipped is not None:
                        actor = audit_actor or (str(flipped[0]), str(flipped[1]))
                        entry = completion_audit_entry(
                            status=status,
                            celex=str(flipped[2]),
                            trigger=_TRIGGER,
                            result=result,
                            reason_code=reason_code,
                        )
                        self._audit_store.record(
                            cur,
                            actor_subject=actor[0],
                            actor_issuer=actor[1],
                            action=entry.action,
                            resource_type=INGESTION_RUN_RESOURCE_TYPE,
                            resource_id=run_id,
                            outcome=entry.outcome,
                            details=entry.details,
                        )
                    conn.commit()
            except psycopg.Error as exc:
                mark_unhealthy(STATE_POSTGRES, error=exc)
                raise IngestionRunPersistenceError from exc
        except (IngestionRunStoreUnavailableError, IngestionRunPersistenceError) as exc:
            self._log("complete_run", "failed", run_id, exc.__cause__ or exc)
            raise
        mark_healthy(STATE_POSTGRES)
        self._log("complete_run", "success" if won else "lost_race", run_id, None)
        return won

    def _log(self, action: str, outcome: str, run_id: str, reason: BaseException | None) -> None:
        extra: dict[str, object] = {}
        if reason is not None:
            extra["reason"] = type(reason).__name__
        # Diagnostics only: a process with no configured default emitter must still work.
        with contextlib.suppress(LoggingLifecycleError):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome=outcome,
                run_id=run_id,
                extra=extra,
                emitter=self._emitter,
            )

    def _connect(self) -> psycopg.Connection[TupleRow]:
        try:
            return connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise IngestionRunStoreUnavailableError from exc


__all__ = ["IngestionRunStore", "PsycopgIngestionRunStore"]
