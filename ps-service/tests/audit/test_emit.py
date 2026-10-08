"""Audit lifecycle primitives: fail-closed opening row, best-effort follow-up row (issue #195)."""

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING

import pytest

from audit._fakes import InMemoryAuditStore
from ps_service.audit import (
    AuditContext,
    AuditDetails,
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTarget,
    AuditUnknownActionError,
    register_audit_action,
)
from ps_service.audit.emit import record_follow_up_row, record_opening_row
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type ReadLines = Callable[[Path], list[dict[str, object]]]
    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

_ACTION = "test.emit_probe"
_ACTOR = ("actor-sub", "actor-iss")
_TARGET = AuditTarget(action=_ACTION, resource_type="probe", resource_id="res-1")


class _ProbeDetails(AuditDetails):
    status: str


with contextlib.suppress(ValueError):  # already registered by an earlier import
    register_audit_action(_ACTION, _ProbeDetails)

_STORE_ERRORS: list[Exception] = [
    AuditPostgresUnavailableError("host=db.internal port=5432"),
    AuditPersistenceError("INSERT failed at /srv/internal/path"),
    AuditUnknownActionError("nope"),
    AuditInvalidDetailsError("bad"),
]


def _context(store: InMemoryAuditStore) -> AuditContext:
    return AuditContext(actor=_ACTOR, store=store)


def test_audit_context_is_frozen_and_carries_actor_and_store() -> None:
    store = InMemoryAuditStore()
    context = _context(store)
    assert context.actor == _ACTOR
    assert context.store is store
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.actor = ("x", "y")  # type: ignore[misc]


def test_record_opening_row_writes_an_applied_row_with_the_given_actor() -> None:
    store = InMemoryAuditStore()
    record_opening_row(
        _context(store),
        _TARGET,
        component="probe",
        details={"status": "started"},
    )
    (row,) = store.rows
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert (row.outcome, row.action, row.resource_id) == ("applied", _ACTION, "res-1")
    assert row.details == {"status": "started"}


@pytest.mark.parametrize("error", _STORE_ERRORS, ids=lambda e: type(e).__name__)
def test_record_opening_row_raises_audit_trail_unavailable_when_the_store_is_down(
    error: Exception,
) -> None:
    store = InMemoryAuditStore(fail_on_outcome={"applied": error})
    with pytest.raises(AuditTrailUnavailableError) as raised:
        record_opening_row(
            _context(store),
            _TARGET,
            component="probe",
            details={"status": "started"},
        )
    assert str(raised.value) == (
        "The audit trail is temporarily unavailable; the operation was not performed."
    )
    assert raised.value.__cause__ is error


def test_record_opening_row_logs_class_name_only_when_the_store_is_down(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(
        fail_on_outcome={"applied": AuditPostgresUnavailableError("host=db.internal")}
    )
    with bind_run_context("run-1"), pytest.raises(AuditTrailUnavailableError):
        record_opening_row(
            _context(store),
            _TARGET,
            component="probe",
            details={"status": "started"},
            emitter=emitter,
        )
    emitter.flush()
    (line,) = read_lines(log_path)
    assert line["action"] == "audit_opening_failed"
    assert line["component"] == "probe"
    assert line["outcome"] == "failed"
    assert line["entity_id"] == "res-1"
    assert line["run_id"] == "run-1"
    assert line["audit_action"] == _ACTION
    assert line["reason"] == "AuditPostgresUnavailableError"
    assert "db.internal" not in str(line)


def test_record_follow_up_row_writes_the_given_outcome_and_returns_true() -> None:
    store = InMemoryAuditStore()
    written = record_follow_up_row(
        _context(store),
        _TARGET,
        component="probe",
        outcome="failed",
        details={"status": "failed"},
    )
    assert written is True
    assert [r.outcome for r in store.rows] == ["failed"]


def test_record_follow_up_row_returns_false_and_logs_bound_run_id_and_exception_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(
        fail_on_outcome={"failed": AuditPersistenceError("INSERT failed host=db.internal")}
    )
    with bind_run_context("run-9"):
        written = record_follow_up_row(
            _context(store),
            _TARGET,
            component="probe",
            outcome="failed",
            details={"status": "failed"},
            emitter=emitter,
        )
    emitter.flush()
    assert written is False
    (line,) = read_lines(log_path)
    assert line["action"] == "audit_terminal_failed"
    assert line["run_id"] == "run-9"
    assert line["entity_id"] == "res-1"
    assert line["audit_action"] == _ACTION
    assert line["reason"] == "AuditPersistenceError"
    assert "db.internal" not in str(line)


@pytest.mark.parametrize("error", _STORE_ERRORS, ids=lambda e: type(e).__name__)
def test_record_follow_up_row_never_raises(error: Exception) -> None:
    store = InMemoryAuditStore(fail_on_outcome={"failed": error})
    assert (
        record_follow_up_row(
            _context(store),
            _TARGET,
            component="probe",
            outcome="failed",
            details={"status": "failed"},
        )
        is False
    )


def test_failure_log_omits_the_resource_id_when_the_target_marks_it_personal_data(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError("x")})
    target = AuditTarget(
        action=_ACTION, resource_type="probe", resource_id="a@example.com", log_resource_id=False
    )
    record_follow_up_row(
        _context(store),
        target,
        component="probe",
        outcome="failed",
        details={"status": "failed"},
        emitter=emitter,
    )
    emitter.flush()
    (line,) = read_lines(log_path)
    assert "entity_id" not in line
    assert "a@example.com" not in str(line)
