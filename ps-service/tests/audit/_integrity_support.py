"""Test-only helpers shared by the live audit-integrity tests (chain walk, head lock, callers)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from audit._integrity_oracle import GENESIS, canonical_v1, chain_hash
from ps_service.runtime_config import define_runtime_config_key, register_runtime_config_key

if TYPE_CHECKING:
    from collections.abc import Generator

    from persistence.provisioned_postgres import Provisioned

ISSUER = "https://issuer.example.com/"


def register_test_key() -> str:
    """Register a uniquely named, always-valid string runtime-config key and return its name."""
    name = f"test.live.{uuid.uuid4().hex[:12]}"
    register_runtime_config_key(
        define_runtime_config_key(
            name, str, validate=lambda value, _config: value, audit_value=lambda value: value
        )
    )
    return name


def chain_rows(prov: Provisioned) -> list[tuple[object, ...]]:
    """Every audit row ordered by `chain_seq`, with its stored chain columns (as superuser)."""
    with prov.superuser_connect(prov.state_db) as conn:
        return conn.execute(
            "SELECT chain_seq, prev_row_hash, row_hash, id, occurred_at, actor_subject, "
            "actor_issuer, action, resource_type, resource_id, outcome, details::text "
            "FROM audit_log.audit_events ORDER BY chain_seq"
        ).fetchall()


def head(prov: Provisioned) -> tuple[object, ...]:
    """The chain head row `(last_chain_seq, last_row_hash)`, read as superuser."""
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(
            "SELECT last_chain_seq, last_row_hash FROM audit_log.audit_chain_head"
        ).fetchone()
    assert row is not None
    return row


def assert_chain_is_intact(prov: Provisioned) -> int:
    """Walk the whole chain with the independent oracle, check the head, return the row count."""
    rows = chain_rows(prov)
    previous = GENESIS
    for position, row in enumerate(rows, start=1):
        seq, prev_hash, row_hash, row_id, occurred_at, *strings, details_text = row
        assert (seq, prev_hash) == (position, previous)
        canonical = canonical_v1(
            chain_seq=position,
            row_id=row_id,
            occurred_at=occurred_at,  # pyright: ignore[reportArgumentType]
            strings=tuple(strings),  # pyright: ignore[reportArgumentType]
            details_text=str(details_text),
        )
        assert row_hash == chain_hash(previous, canonical)
        previous = str(row_hash)
    assert head(prov) == (len(rows), previous)
    return len(rows)


@contextmanager
def head_row_locked(prov: Provisioned) -> Generator[None]:
    """Hold `FOR UPDATE` on the chain head in an open superuser transaction, then release it.

    Any audit insert by another session blocks behind it, which is how the tests make the chain
    trigger fail without a production hook.
    """
    conn = psycopg.connect(
        host=prov.host, port=prov.port, user=prov.superuser, dbname=prov.state_db
    )
    try:
        conn.execute("SELECT 1 FROM audit_log.audit_chain_head FOR UPDATE")
        yield
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def role_statement_timeout(prov: Provisioned, value: str) -> Generator[None]:
    """Give the application role a `statement_timeout`, restored afterwards (scratch cluster)."""
    role = sql.Identifier(prov.state_user)
    with prov.superuser_connect() as conn:
        conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, value))
    try:
        yield
    finally:
        with prov.superuser_connect() as conn:
            conn.execute(sql.SQL("ALTER ROLE {} RESET statement_timeout").format(role))


@contextmanager
def chain_function_lock_timeout(prov: Provisioned, value: str) -> Generator[None]:
    """Shorten the chain trigger's function-level `lock_timeout`, restored to 10s afterwards."""
    with prov.superuser_connect(prov.state_db) as conn:
        conn.execute(
            sql.SQL("ALTER FUNCTION audit_log.audit_events_chain() SET lock_timeout = {}").format(
                value
            )
        )
    try:
        yield
    finally:
        with prov.superuser_connect(prov.state_db) as conn:
            conn.execute("ALTER FUNCTION audit_log.audit_events_chain() SET lock_timeout = '10s'")
