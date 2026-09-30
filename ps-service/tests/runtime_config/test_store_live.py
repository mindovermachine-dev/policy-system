"""`postgres_live` tests for `PsycopgRuntimeConfigStore` against a real Postgres (issue #130).

Proves what only a real database can: the state write and its `audit_events` row commit or
roll back together (AC-BI-011), exactly one audit row per `set`/`reset` with typed details
(AC-BI-012), nothing is written for a rejected `set` (AC-BI-005), and concurrent writers on
one key record a consistent old-value chain (advisory lock, CHANGES.md F3).

Deselected by default -- run with `uv run pytest -m postgres_live` against a reachable
`PS_STATE_POSTGRES_*` instance. The shared `public` schema is reused across the session, so
every test registers a uniquely named key and only inspects its own rows.
"""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING, Literal

import psycopg
import pytest
from psycopg.types.json import Json

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import PsycopgAuditStore
from ps_service.audit.models import AuditQueryFilters
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import ServiceConfig, load_config
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.runtime_config import (
    MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR,
)
from ps_service.runtime_config import (
    PsycopgRuntimeConfigStore,
    RuntimeConfigInvalidValueError,
    RuntimeConfigPersistenceError,
    define_runtime_config_key,
    register_runtime_config_key,
)
from runtime_config._fakes import RecordingAuditStore

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from psycopg.rows import TupleRow

    from ps_service.audit.models import AuditQueryPage

# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
]

_ISSUER = "https://issuer.example.com/"


class _NotAllowedError(Exception):
    """Validator failure type the test keys declare."""


def _config() -> ServiceConfig:
    config = load_config()
    assert config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    return config


def _migrated_config() -> ServiceConfig:
    config = _config()
    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    return config


def _reject_forbidden(value: str, _config: ServiceConfig) -> str:
    if value == "forbidden":
        message = "value is forbidden"
        raise _NotAllowedError(message)
    return value


def _strip_query(value: str) -> str:
    return value.split("?", 1)[0]


def _new_key(*, project: Callable[[str], str] | None = None) -> str:
    name = f"test.live.{uuid.uuid4().hex[:12]}"
    register_runtime_config_key(
        define_runtime_config_key(
            name,
            str,
            validate=_reject_forbidden,
            audit_value=project or (lambda value: value),
            validation_errors=(_NotAllowedError,),
        )
    )
    return name


def _store(config: ServiceConfig) -> PsycopgRuntimeConfigStore:
    return PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))


def _actor() -> tuple[str, str]:
    return (f"actor-{uuid.uuid4().hex[:8]}", _ISSUER)


def _config_row(config: ServiceConfig, key: str) -> object | None:
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM runtime_config WHERE key = %(key)s", {"key": key})
        row = cur.fetchone()
    return None if row is None else row[0]


def _audit_rows(
    config: ServiceConfig, key: str
) -> list[tuple[str, str, str, str, str, dict[str, object]]]:
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT actor_subject, actor_issuer, action, resource_type, outcome, details "
            "FROM audit_events WHERE resource_id = %(key)s ORDER BY occurred_at, id",
            {"key": key},
        )
        rows: list[tuple[str, str, str, str, str, dict[str, object]]] = []
        for actor_subject, actor_issuer, action, resource_type, outcome, details in cur.fetchall():
            rows.append((actor_subject, actor_issuer, action, resource_type, outcome, details))
        return rows


class _RaisingAuditStore(RecordingAuditStore):
    """`AuditStore` whose `record` fails like a broken `audit_events` insert would."""

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
        del cur, actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise psycopg.errors.OperationalError("simulated audit_events insert failure")


pytestmark = pytest.mark.postgres_live


def test_set_then_get_round_trips_value() -> None:
    config = _migrated_config()
    key = _new_key()

    _store(config).set(key, "first", actor=_actor())

    assert _store(config).get(key) == "first"


def test_get_returns_none_when_no_row_exists() -> None:
    config = _migrated_config()

    assert _store(config).get(_new_key()) is None


def test_set_writes_exactly_one_audit_row_with_actor_key_old_new_in_typed_details() -> None:
    config = _migrated_config()
    key = _new_key()
    actor = _actor()
    store = _store(config)
    store.set(key, "first", actor=_actor())

    store.set(key, "second", actor=actor)

    rows = _audit_rows(config, key)
    assert len(rows) == 2
    assert rows[1] == (
        actor[0],
        actor[1],
        "runtime_config.set",
        "runtime_config",
        "applied",
        {"key": key, "old_value": "first", "new_value": "second"},
    )
    assert rows[0][5] == {"key": key, "new_value": "first"}


def test_set_of_an_identical_value_still_writes_one_audit_row() -> None:
    config = _migrated_config()
    key = _new_key()
    store = _store(config)
    store.set(key, "same", actor=_actor())

    store.set(key, "same", actor=_actor())

    rows = _audit_rows(config, key)
    assert [row[5] for row in rows] == [
        {"key": key, "new_value": "same"},
        {"key": key, "old_value": "same", "new_value": "same"},
    ]


def test_reset_removes_row_and_writes_exactly_one_audit_row() -> None:
    config = _migrated_config()
    key = _new_key()
    actor = _actor()
    store = _store(config)
    store.set(key, "value", actor=_actor())

    store.reset(key, actor=actor)

    assert _config_row(config, key) is None
    reset_rows = [row for row in _audit_rows(config, key) if row[2] == "runtime_config.reset"]
    assert reset_rows == [
        (
            actor[0],
            actor[1],
            "runtime_config.reset",
            "runtime_config",
            "applied",
            {"key": key, "old_value": "value"},
        )
    ]


def test_reset_absent_key_is_success_and_writes_one_audit_row() -> None:
    config = _migrated_config()
    key = _new_key()

    _store(config).reset(key, actor=_actor())

    rows = _audit_rows(config, key)
    assert [(row[2], row[5]) for row in rows] == [("runtime_config.reset", {"key": key})]


def test_reset_absent_key_details_omit_old_value() -> None:
    config = _migrated_config()
    key = _new_key()

    _store(config).reset(key, actor=_actor())

    (row,) = _audit_rows(config, key)
    assert "old_value" not in row[5]


def test_rejected_set_writes_no_runtime_config_or_audit_row() -> None:
    config = _migrated_config()
    key = _new_key()

    with pytest.raises(RuntimeConfigInvalidValueError):
        _store(config).set(key, "forbidden", actor=_actor())

    assert _config_row(config, key) is None
    assert _audit_rows(config, key) == []


def test_set_rolls_back_state_when_audit_insert_fails() -> None:
    config = _migrated_config()
    key = _new_key()
    store = PsycopgRuntimeConfigStore(config, audit_store=_RaisingAuditStore())

    with pytest.raises(RuntimeConfigPersistenceError):
        store.set(key, "value", actor=_actor())

    assert _config_row(config, key) is None
    assert _audit_rows(config, key) == []


def test_reset_rolls_back_state_when_audit_insert_fails() -> None:
    config = _migrated_config()
    key = _new_key()
    _store(config).set(key, "keep-me", actor=_actor())
    store = PsycopgRuntimeConfigStore(config, audit_store=_RaisingAuditStore())

    with pytest.raises(RuntimeConfigPersistenceError):
        store.reset(key, actor=_actor())

    assert _config_row(config, key) == "keep-me"
    assert [row[2] for row in _audit_rows(config, key)] == ["runtime_config.set"]


def test_set_rolls_back_audit_row_when_state_write_fails() -> None:
    config = _migrated_config()
    name = f"test.live.{uuid.uuid4().hex[:12]}"
    register_runtime_config_key(
        # NaN is a valid `float` but not valid `jsonb`: the state write itself is what fails.
        define_runtime_config_key(
            name, float, validate=lambda value, _c: value, audit_value=lambda _value: "nan"
        )
    )

    with pytest.raises(RuntimeConfigPersistenceError):
        _store(config).set(name, float("nan"), actor=_actor())

    assert _config_row(config, name) is None
    assert _audit_rows(config, name) == []


def test_get_rejects_row_with_invalid_shape_written_out_of_band() -> None:
    config = _migrated_config()
    key = _new_key()
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runtime_config (key, value) VALUES (%(key)s, %(value)s)",
            {"key": key, "value": Json({"not": "a string"})},
        )

    with pytest.raises(RuntimeConfigInvalidValueError):
        _store(config).get(key)


def test_get_rejects_out_of_band_row_that_fails_the_key_validator() -> None:
    config = _migrated_config()
    key = _new_key()
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runtime_config (key, value) VALUES (%(key)s, %(value)s)",
            {"key": key, "value": Json("forbidden")},
        )

    with pytest.raises(RuntimeConfigInvalidValueError):
        _store(config).get(key)


def test_set_over_a_corrupt_row_repairs_it_and_omits_the_unvalidated_old_value() -> None:
    config = _migrated_config()
    key = _new_key()
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runtime_config (key, value) VALUES (%(key)s, %(value)s)",
            {"key": key, "value": Json({"secret": "blob"})},
        )

    _store(config).set(key, "repaired", actor=_actor())

    assert _store(config).get(key) == "repaired"
    (row,) = _audit_rows(config, key)
    assert row[5] == {"key": key, "new_value": "repaired"}


def test_audit_details_carry_the_key_projection_not_the_raw_value() -> None:
    config = _migrated_config()
    key = _new_key(project=_strip_query)

    _store(config).set(key, "https://host/path?token=hunter2", actor=_actor())

    (row,) = _audit_rows(config, key)
    assert row[5] == {"key": key, "new_value": "https://host/path"}
    assert _config_row(config, key) == "https://host/path?token=hunter2"


def test_set_row_is_readable_through_the_audit_store_query_used_by_list_audit_events() -> None:
    config = _migrated_config()
    key = _new_key()
    actor = _actor()
    _store(config).set(key, "value", actor=actor)

    page: AuditQueryPage = PsycopgAuditStore(config).query(
        filters=AuditQueryFilters(resource_type="runtime_config", resource_id=key),
        cursor=None,
        page_size=10,
    )

    assert [(event.action, event.actor_subject, event.details) for event in page.events] == [
        ("runtime_config.set", actor[0], {"key": key, "new_value": "value"})
    ]


def _run_concurrently(*calls: Callable[[], None]) -> None:
    barrier = threading.Barrier(len(calls))
    errors: list[BaseException] = []

    def _wrap(call: Callable[[], None]) -> None:
        try:
            barrier.wait()
            call()
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the asserting thread below
            errors.append(exc)

    threads = [threading.Thread(target=_wrap, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_concurrent_set_and_reset_on_absent_key_record_consistent_old_values() -> None:
    config = _migrated_config()
    for _ in range(10):
        key = _new_key()
        _run_concurrently(
            lambda key=key: _store(config).set(key, "a", actor=_actor()),
            lambda key=key: _store(config).reset(key, actor=_actor()),
        )

        rows = {row[2]: row[5] for row in _audit_rows(config, key)}
        final = _config_row(config, key)
        reset_saw_the_set = rows["runtime_config.reset"].get("old_value") == "a"
        # Either the set committed first (reset removed it) or the reset ran first (set stays).
        assert (reset_saw_the_set and final is None) or (not reset_saw_the_set and final == "a")


def test_concurrent_sets_on_absent_key_chain_old_values_consistently() -> None:
    config = _migrated_config()
    for _ in range(10):
        key = _new_key()
        _run_concurrently(
            lambda key=key: _store(config).set(key, "a", actor=_actor()),
            lambda key=key: _store(config).set(key, "b", actor=_actor()),
        )

        details = [row[5] for row in _audit_rows(config, key)]
        first = next(d for d in details if "old_value" not in d)
        second = next(d for d in details if "old_value" in d)
        assert second["old_value"] == first["new_value"]
        assert _config_row(config, key) == second["new_value"]
