"""Fast tests for `PsycopgRuntimeConfigStore` (issue #130, Slice 3).

Everything here is decided before, or instead of, a successful connection: input rejection
(AC-BI-005), the fixed no-detail unavailable error (AC-BI-010 groundwork) and semantic
logging. The transactional behaviour against a real Postgres lives in `test_store_live.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.config import ServiceConfig
from ps_service.runtime_config import (
    PsycopgRuntimeConfigStore,
    RuntimeConfigInvalidValueError,
    RuntimeConfigUnavailableError,
    RuntimeConfigUnknownKeyError,
    define_runtime_config_key,
    register_runtime_config_key,
)
from runtime_config._fakes import RecordingAuditStore

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines

    from ps_service.logging import LogEmitter

_ACTOR = ("actor-subject", "https://issuer.example.com/")
_POSITIVE_INT_KEY = "test.store.positive_int"
_UNREGISTERED_KEY = "test.store.never_registered"


class _NotPositiveError(Exception):
    """Validator failure type a key declares through `validation_errors`."""


def _require_positive(value: int, _config: ServiceConfig) -> int:
    if value <= 0:
        message = "value must be positive"
        raise _NotPositiveError(message)
    return value


register_runtime_config_key(
    define_runtime_config_key(
        _POSITIVE_INT_KEY,
        int,
        validate=_require_positive,
        audit_value=lambda value: value,
        validation_errors=(_NotPositiveError,),
    )
)


def _config(*, host: str | None, port: int = 5432) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        state_postgres_host=host,
        state_postgres_port=port,
        state_postgres_database="ps_state",
        state_postgres_user="ps_state",
        state_postgres_password="unused",
    )


def _store(
    *, host: str | None, port: int = 5432, emitter: LogEmitter | None = None
) -> PsycopgRuntimeConfigStore:
    return PsycopgRuntimeConfigStore(
        _config(host=host, port=port), audit_store=RecordingAuditStore(), emitter=emitter
    )


def test_set_unregistered_key_raises_before_any_connection() -> None:
    # An unconfigured store would raise `RuntimeConfigUnavailableError` on any connect
    # attempt, so getting the unknown-key error proves no connection was tried.
    with pytest.raises(RuntimeConfigUnknownKeyError):
        _store(host=None).set(_UNREGISTERED_KEY, 1, actor=_ACTOR)


def test_set_invalid_value_raises_before_any_connection() -> None:
    with pytest.raises(RuntimeConfigInvalidValueError, match="value must be positive"):
        _store(host=None).set(_POSITIVE_INT_KEY, -3, actor=_ACTOR)


def test_set_value_of_the_wrong_type_raises_before_any_connection() -> None:
    with pytest.raises(RuntimeConfigInvalidValueError):
        _store(host=None).set(_POSITIVE_INT_KEY, "12", actor=_ACTOR)


def test_reset_unregistered_key_raises_before_any_connection() -> None:
    with pytest.raises(RuntimeConfigUnknownKeyError):
        _store(host=None).reset(_UNREGISTERED_KEY, actor=_ACTOR)


def test_get_unregistered_key_raises_unknown_key_error() -> None:
    with pytest.raises(RuntimeConfigUnknownKeyError):
        _store(host=None).get(_UNREGISTERED_KEY)


def test_get_unreachable_store_raises_fixed_message_without_host_port() -> None:
    store = _store(host="127.0.0.1", port=59999)

    with pytest.raises(RuntimeConfigUnavailableError) as exc_info:
        store.get(_POSITIVE_INT_KEY)

    message = str(exc_info.value)
    assert message == "The runtime configuration store is temporarily unavailable."
    assert "127.0.0.1" not in message
    assert "59999" not in message
    assert "psycopg" not in message.lower()


def test_set_unreachable_store_raises_the_same_fixed_message() -> None:
    with pytest.raises(RuntimeConfigUnavailableError) as exc_info:
        _store(host="127.0.0.1", port=59999).set(_POSITIVE_INT_KEY, 5, actor=_ACTOR)

    assert str(exc_info.value) == "The runtime configuration store is temporarily unavailable."


def test_unconfigured_store_raises_unavailable_error() -> None:
    with pytest.raises(RuntimeConfigUnavailableError):
        _store(host=None).get(_POSITIVE_INT_KEY)


def test_rejected_set_logs_key_and_reason_but_never_the_value(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    with pytest.raises(RuntimeConfigInvalidValueError):
        _store(host=None, emitter=emitter).set(_POSITIVE_INT_KEY, -987654, actor=_ACTOR)
    emitter.flush()

    (entry,) = read_lines(log_path)
    assert entry["component"] == "runtime_config"
    assert entry["action"] == "set"
    assert entry["outcome"] == "rejected"
    assert entry["key"] == _POSITIVE_INT_KEY
    assert entry["reason"] == "RuntimeConfigInvalidValueError"
    assert "-987654" not in str(entry)


def test_failed_get_logs_failed_outcome_with_exception_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    with pytest.raises(RuntimeConfigUnavailableError):
        _store(host="127.0.0.1", port=59999, emitter=emitter).get(_POSITIVE_INT_KEY)
    emitter.flush()

    (entry,) = read_lines(log_path)
    assert (entry["action"], entry["outcome"]) == ("get", "failed")
    assert entry["reason"] == "OperationalError"
    assert "59999" not in str(entry)
