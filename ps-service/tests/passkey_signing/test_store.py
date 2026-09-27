"""Tests for `ps_service.passkey_signing.store` (issue #131, PLAN.md Slice 1).

Unit tests below exercise `FakePendingApprovalStore` (`_fakes.py`), a
`Protocol`-typed in-memory double -- proving the `PendingApprovalStore`
contract is sound without a real Postgres (PLAN.md §0.6's own established
`falkordb_live` split: unit tests against a fake store, one `postgres_live`
test proves the real round-trip). The `postgres_live`-marked test at the
bottom of this file is that proof: it applies migrations, inserts a real row
via `PsycopgPendingApprovalStore`, and reads it back from a real Postgres
instance -- something the fake cannot prove (it never executes SQL).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from passkey_signing._fakes import FakePendingApprovalStore
from ps_service.config import load_config
from ps_service.passkey_signing.migration_runner import apply_pending_migrations
from ps_service.passkey_signing.store import (
    PendingApprovalStore,
    PsycopgPendingApprovalStore,
    connect_from_config,
)

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow

_TOOL_NAME = "near_misses_resolve"
_NORMALIZED_ARGS: dict[str, object] = {"review_id": "review-123", "decision": "merge"}
_ACTOR_SUBJECT = "actor-sub-1"
_ACTOR_ISSUER = "https://issuer.example.com/"
_DISPLAY_SUMMARY: dict[str, object] = {
    "kind": "capability",
    "incoming_text": "incoming text",
    "nearest_existing_text": "existing text",
    "similarity": 0.92,
}


def _build_store() -> PendingApprovalStore:
    """Return a fresh `FakePendingApprovalStore`, typed as the `Protocol` it satisfies."""
    return FakePendingApprovalStore()


def _create(store: PendingApprovalStore) -> tuple[PendingApprovalRow, str]:
    return store.create_pending_approval(
        tool_name=_TOOL_NAME,
        normalized_args=_NORMALIZED_ARGS,
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary=_DISPLAY_SUMMARY,
    )


def test_create_pending_approval_returns_a_row_and_a_code_matching_its_hash() -> None:
    store = _build_store()

    row, code = _create(store)

    assert row.code_hash == hashlib.sha256(code.encode()).digest()
    assert row.tool_name == _TOOL_NAME
    assert row.normalized_args == _NORMALIZED_ARGS
    assert row.actor_subject == _ACTOR_SUBJECT
    assert row.actor_issuer == _ACTOR_ISSUER
    assert row.display_summary == _DISPLAY_SUMMARY
    assert row.status == "pending"
    assert row.outcome is None


def test_get_by_id_returns_the_created_row() -> None:
    store = _build_store()
    created, _code = _create(store)

    found = store.get_by_id(created.id)

    assert found == created


def test_get_by_id_returns_none_for_an_unknown_id() -> None:
    store = _build_store()

    assert store.get_by_id("does-not-exist") is None


def test_get_by_code_hash_returns_the_created_row() -> None:
    store = _build_store()
    created, code = _create(store)

    found = store.get_by_code_hash(hashlib.sha256(code.encode()).digest())

    assert found == created


def test_get_by_code_hash_returns_none_for_an_unknown_hash() -> None:
    store = _build_store()

    assert store.get_by_code_hash(b"\x00" * 32) is None


def test_expiry_is_created_at_plus_fifteen_minutes() -> None:
    store = _build_store()

    row, _code = _create(store)

    assert row.expires_at - row.created_at == timedelta(minutes=15)


def test_two_created_rows_get_distinct_ids_codes_and_nonces() -> None:
    store = _build_store()

    first, first_code = _create(store)
    second, second_code = _create(store)

    assert first.id != second.id
    assert first_code != second_code
    assert first.code_hash != second.code_hash
    assert first.nonce != second.nonce


# --- postgres_live (issue #131, PLAN.md Slice 1) ----------------------------

_LIVE_TEST_TOOL_NAME = "near_misses_resolve_slice1_store_live_test"


@pytest.mark.postgres_live
def test_create_and_read_back_a_pending_approval_against_a_real_postgres_instance() -> None:
    """AC-BI-006/AC-BI-008/AC-BI-014 against a REAL PostgreSQL instance.

    Applies migrations, inserts a real row via `PsycopgPendingApprovalStore`,
    reads it back both by `id` and by `code_hash`, and confirms
    `schema_migrations` recorded `0001_pending_approvals.sql` as applied.
    Writes into (and cleans up) rows scoped to a dedicated `tool_name`
    marker, never touching any other data that may already live in the
    configured database.

    Requires a reachable Postgres matching `PS_PASSKEYSIGNING_POSTGRES_*`
    (e.g. the devcontainer's `postgres` service) -- deselected from the
    default run by the `postgres_live` marker (`pyproject.toml`).
    """
    config = load_config()
    assert config.passkey_signing_postgres_host is not None, (
        "postgres_live requires PS_PASSKEYSIGNING_POSTGRES_HOST to be set"
    )

    with connect_from_config(config) as setup_conn:
        apply_pending_migrations(setup_conn)
        with setup_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pending_approvals WHERE tool_name = %(tool_name)s",
                {"tool_name": _LIVE_TEST_TOOL_NAME},
            )

    store = PsycopgPendingApprovalStore(config)
    try:
        row, code = store.create_pending_approval(
            tool_name=_LIVE_TEST_TOOL_NAME,
            normalized_args=_NORMALIZED_ARGS,
            actor_subject=_ACTOR_SUBJECT,
            actor_issuer=_ACTOR_ISSUER,
            display_summary=_DISPLAY_SUMMARY,
        )

        assert row.code_hash == hashlib.sha256(code.encode()).digest()
        assert row.expires_at - row.created_at == timedelta(minutes=15)

        found_by_id = store.get_by_id(row.id)
        assert found_by_id == row

        found_by_code_hash = store.get_by_code_hash(row.code_hash)
        assert found_by_code_hash == row

        with connect_from_config(config) as verify_conn, verify_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = %(filename)s",
                {"filename": "0001_pending_approvals.sql"},
            )
            assert cur.fetchone() is not None
    finally:
        with connect_from_config(config) as cleanup_conn, cleanup_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pending_approvals WHERE tool_name = %(tool_name)s",
                {"tool_name": _LIVE_TEST_TOOL_NAME},
            )
