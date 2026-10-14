"""PostgreSQL persistence for `runtime_config`, with same-transaction audit.

`RuntimeConfigStore` is the `Protocol` consumers (`curated_source`, the MCP tools) depend on;
`PsycopgRuntimeConfigStore` is the real implementation. Every method checks the key against
the registry (`ps_service.runtime_config.registry`) *before* opening a connection, so an
unregistered key or an invalid value never touches the database.

`set`/`reset` run in one transaction: an advisory lock on the key (taken before the old
value is read, so it also serialises writers on a key that has no row yet), the old-value
read, the upsert/delete, and the `audit_events` row written through `AuditStore.record` on
the same cursor, with no config-specific hook in `audit`. Any failure rolls the whole
transaction back.

Failure discipline (fail closed): connection or read failures raise
`RuntimeConfigUnavailableError`, write/audit failures `RuntimeConfigPersistenceError`; both
carry fixed messages, never host/port/driver text. Log entries carry the key and the
exception class name only -- never the value.

Audit is unconditional: a `reset` of a key with no row and a `set` of an identical value each still
write exactly one audit row (a `reset`'s `old_value` is then absent).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

import psycopg
from psycopg.types.json import Json

from ps_service.dependency_health import STATE_POSTGRES, mark_healthy, mark_unhealthy
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError, connect_from_config
from ps_service.runtime_config import audit_actions
from ps_service.runtime_config.errors import (
    RuntimeConfigError,
    RuntimeConfigInvalidValueError,
    RuntimeConfigPersistenceError,
    RuntimeConfigUnavailableError,
)
from ps_service.runtime_config.registry import (
    prepare_runtime_config_value,
    require_runtime_config_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg.rows import TupleRow

    from ps_service.audit import AuditStore
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter
    from ps_service.runtime_config.registry import AuditScalar, RuntimeConfigKey

_COMPONENT = "runtime_config"
_ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(hashtext(%(lock_key)s))"
_SELECT_VALUE = "SELECT value FROM runtime_config WHERE key = %(key)s"
_UPSERT = (
    "INSERT INTO runtime_config (key, value) VALUES (%(key)s, %(value)s) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
)
_DELETE = "DELETE FROM runtime_config WHERE key = %(key)s"


class RuntimeConfigStore(Protocol):
    """Persistence seam for runtime config values, constructor-injected wherever needed.

    Every method validates the key against the registry before touching storage. `set` and
    `reset` are the only writers of the `runtime_config` table and each writes one audit row
    in the same transaction.
    """

    def get(self, key: str) -> object | None:
        """Return the stored, re-validated value for `key`.

        Args:
            key: Registered key name.

        Returns:
            The stored value after type check and validation, or `None` when no row exists.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered.
            RuntimeConfigUnavailableError: the store could not be reached or read (fail closed).
            RuntimeConfigInvalidValueError: the stored row fails validation for `key`.
        """
        ...

    def set(self, key: str, value: object, *, actor: tuple[str, str]) -> None:
        """Validate `value`, then upsert it and write one `runtime_config.set` audit row.

        Args:
            key: Registered key name.
            value: New value; must pass the key's strict type check and validator.
            actor: The acting principal's `(subject, issuer)`, recorded on the audit row.

        Returns:
            None.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered; nothing was written.
            RuntimeConfigInvalidValueError: `value` failed the key's type check or validator,
                rejected before any connection is opened; nothing was written.
            RuntimeConfigUnavailableError: the store could not be reached.
            RuntimeConfigPersistenceError: the write or its audit insert failed; rolled back.
            AuditUnknownActionError: the audit action is not registered (a wiring defect).
            AuditInvalidDetailsError: the audit details failed their model (a wiring defect).

        Side Effects:
            Takes a per-key advisory lock, upserts the `runtime_config` row, inserts one
            `audit_events` row in the same transaction, updates the state-Postgres health
            status, and emits one log entry carrying the key and outcome, never the value.
        """
        ...

    def reset(self, key: str, *, actor: tuple[str, str]) -> None:
        """Delete `key`'s row (if any) and write one `runtime_config.reset` audit row.

        The audit row is written also when the key has no row.

        Args:
            key: Registered key name.
            actor: The acting principal's `(subject, issuer)`, recorded on the audit row.

        Returns:
            None.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered.
            RuntimeConfigUnavailableError: the store could not be reached.
            RuntimeConfigPersistenceError: the delete or its audit insert failed; rolled back.
            AuditUnknownActionError: the audit action is not registered (a wiring defect).
            AuditInvalidDetailsError: the audit details failed their model (a wiring defect).

        Side Effects:
            Takes a per-key advisory lock, deletes the `runtime_config` row if present, inserts
            one `audit_events` row in the same transaction, updates the state-Postgres health
            status, and emits one log entry carrying the key and outcome.
        """
        ...


class PsycopgRuntimeConfigStore:
    """`RuntimeConfigStore` backed by PostgreSQL via `psycopg[binary]`.

    Every method opens, uses and closes its own connection (`connect_from_config`) -- no
    pool, no cached connection held across calls.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        audit_store: AuditStore,
        emitter: LogEmitter | None = None,
    ) -> None:
        """Keep the collaborators; no connection is opened.

        Args:
            config: Process configuration holding the state-Postgres coordinates and handed
                to key validators.
            audit_store: Store used to write the audit row inside each write transaction.
            emitter: Log emitter for outcome entries; `None` uses the process default, and
                logging is skipped when none is configured.
        """
        self._config = config
        self._audit_store = audit_store
        self._emitter = emitter

    def get(self, key: str) -> object | None:
        """Read one key from Postgres and re-validate it.

        Args:
            key: Registered key name.

        Returns:
            The stored value after type check and validation, or `None` when no row exists.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered.
            RuntimeConfigUnavailableError: Postgres could not be reached or read.
            RuntimeConfigInvalidValueError: the stored row fails validation for `key`.

        Side Effects:
            Opens and closes one connection, updates the state-Postgres health status, and
            emits one log entry carrying the key and outcome.
        """
        return self._guarded("get", key, self._get)

    def set(self, key: str, value: object, *, actor: tuple[str, str]) -> None:
        """Validate, upsert and audit one key in one transaction.

        Args:
            key: Registered key name.
            value: New value; must pass the key's strict type check and validator.
            actor: The acting principal's `(subject, issuer)`, recorded on the audit row.

        Returns:
            None.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered; nothing was written.
            RuntimeConfigInvalidValueError: `value` failed the key's type check or validator;
                nothing was written.
            RuntimeConfigUnavailableError: Postgres could not be reached.
            RuntimeConfigPersistenceError: the write or its audit insert failed; rolled back.
            AuditUnknownActionError: the audit action is not registered (a wiring defect).
            AuditInvalidDetailsError: the audit details failed their model (a wiring defect).

        Side Effects:
            Takes a per-key advisory lock, upserts the `runtime_config` row and inserts one
            `audit_events` row in one transaction, updates the state-Postgres health status,
            and emits one log entry carrying the key and outcome, never the value.
        """

        def _run(registered: RuntimeConfigKey) -> None:
            validated = prepare_runtime_config_value(self._config, registered, value)
            self._write("set", registered, actor=actor, new_value=validated)

        self._guarded("set", key, _run)

    def reset(self, key: str, *, actor: tuple[str, str]) -> None:
        """Delete and audit one key in one transaction.

        Args:
            key: Registered key name.
            actor: The acting principal's `(subject, issuer)`, recorded on the audit row.

        Returns:
            None.

        Raises:
            RuntimeConfigUnknownKeyError: `key` is not registered.
            RuntimeConfigUnavailableError: Postgres could not be reached.
            RuntimeConfigPersistenceError: the delete or its audit insert failed; rolled back.
            AuditUnknownActionError: the audit action is not registered (a wiring defect).
            AuditInvalidDetailsError: the audit details failed their model (a wiring defect).

        Side Effects:
            Takes a per-key advisory lock, deletes the `runtime_config` row if present and
            inserts one `audit_events` row in one transaction, updates the state-Postgres
            health status, and emits one log entry carrying the key and outcome.
        """
        self._guarded(
            "reset", key, lambda registered: self._write("reset", registered, actor=actor)
        )

    def _guarded[R](self, action: str, key: str, run: Callable[[RuntimeConfigKey], R]) -> R:
        """Resolve `key`, run `run` on it, and log the outcome (success, rejected or failed)."""
        try:
            result = run(require_runtime_config_key(key))
        except (RuntimeConfigUnavailableError, RuntimeConfigPersistenceError) as exc:
            self._log(action, key, "failed", exc.__cause__ or exc)
            raise
        except RuntimeConfigError as exc:
            self._log(action, key, "rejected", exc)
            raise
        self._log(action, key, "success", None)
        return result

    def _log(self, action: str, key: str, outcome: str, reason: BaseException | None) -> None:
        """Emit one log entry with the key and the reason's class name, never a value."""
        extra: dict[str, object] = {"key": key}
        if reason is not None:
            extra["reason"] = type(reason).__name__
        # Diagnostics only: a process with no configured default emitter must still work.
        with contextlib.suppress(LoggingLifecycleError):
            emit_log_entry(
                component=_COMPONENT,
                action=action,
                outcome=outcome,
                extra=extra,
                emitter=self._emitter,
            )

    def _connect(self) -> psycopg.Connection[TupleRow]:
        """Open a state-Postgres connection, or raise `RuntimeConfigUnavailableError`."""
        try:
            return connect_from_config(self._config)
        except (StatePostgresConnectionError, psycopg.Error) as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise RuntimeConfigUnavailableError from exc

    def _get(self, key: RuntimeConfigKey) -> object | None:
        """Read and re-validate the stored value of `key`; `None` when no row exists."""
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(_SELECT_VALUE, {"key": key.name})
                row = cur.fetchone()
        except psycopg.Error as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise RuntimeConfigUnavailableError from exc
        mark_healthy(STATE_POSTGRES)
        if row is None:
            return None
        return prepare_runtime_config_value(self._config, key, row[0])

    def _write(
        self,
        action: str,
        key: RuntimeConfigKey,
        *,
        actor: tuple[str, str],
        new_value: object | None = None,
    ) -> None:
        """Run the locked read, upsert-or-delete and audit insert of `set`/`reset` atomically."""
        conn = self._connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(_ADVISORY_LOCK, {"lock_key": f"ps_runtime_config:{key.name}"})
                cur.execute(_SELECT_VALUE, {"key": key.name})
                stored = cur.fetchone()
                old_value = self._project_stored(key, None if stored is None else stored[0])
                details: dict[str, object]
                if action == "set":
                    cur.execute(_UPSERT, {"key": key.name, "value": Json(new_value)})
                    details = {"key": key.name, "new_value": key.project(new_value)}
                    audit_action = audit_actions.RUNTIME_CONFIG_SET_ACTION
                else:
                    cur.execute(_DELETE, {"key": key.name})
                    details = {"key": key.name}
                    audit_action = audit_actions.RUNTIME_CONFIG_RESET_ACTION
                if old_value is not None:
                    details["old_value"] = old_value
                self._audit_store.record(
                    cur,
                    actor_subject=actor[0],
                    actor_issuer=actor[1],
                    action=audit_action,
                    resource_type=audit_actions.RUNTIME_CONFIG_RESOURCE_TYPE,
                    resource_id=key.name,
                    outcome="applied",
                    details=details,
                )
                conn.commit()
        except psycopg.Error as exc:
            mark_unhealthy(STATE_POSTGRES, error=exc)
            raise RuntimeConfigPersistenceError from exc
        mark_healthy(STATE_POSTGRES)

    def _project_stored(self, key: RuntimeConfigKey, raw: object | None) -> AuditScalar:
        """The audit projection of the previous stored value, or `None` when there is none.

        A stored row that fails validation (hand-edited out of band) is treated as having
        no old value: an unvalidated blob must never reach an audit row's `details`, and
        refusing to overwrite it would leave the key impossible to repair through the tools.
        """
        if raw is None:
            return None
        try:
            return key.project(prepare_runtime_config_value(self._config, key, raw))
        except RuntimeConfigInvalidValueError:
            return None


__all__ = ["PsycopgRuntimeConfigStore", "RuntimeConfigStore"]
