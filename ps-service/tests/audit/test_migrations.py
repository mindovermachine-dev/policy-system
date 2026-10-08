"""Tests for audit migration 0002 (issue #195, Slice 11): `details` filter expression indexes.

Fast, file-level checks (no Postgres); the live application of the file is covered by
`tests/persistence/test_migration_runner.py` and `tests/audit/test_store_query.py`.
"""

from __future__ import annotations

import re

from ps_service.audit import MIGRATIONS_DIR
from ps_service.audit.models import AUDIT_DETAILS_FILTER_KEYS
from ps_service.persistence.migration_runner import (
    _split_statements,  # pyright: ignore[reportPrivateUsage]  -- the same comment-semicolon guard the other components' migration tests use
)

_FILE = MIGRATIONS_DIR / "0002_audit_events_details_indexes.sql"


def _code_only() -> str:
    return re.sub(r"--[^\n]*", "", _FILE.read_text(encoding="utf-8"))


def test_0002_creates_one_expression_index_per_allow_listed_key() -> None:
    indexes = re.findall(
        r"CREATE INDEX (\w+)\s+ON audit_events \(\(details ->> '(\w+)'\)\);", _code_only()
    )

    assert indexes == [
        (f"audit_events_details_{key}_idx", key) for key in AUDIT_DETAILS_FILTER_KEYS
    ]


def test_0002_has_no_semicolon_inside_a_comment() -> None:
    sql = _FILE.read_text(encoding="utf-8")

    assert len(_split_statements(sql)) == _code_only().count(";")


def test_0002_carries_the_append_only_rule_header() -> None:
    assert "append-only from the first deployment onward" in _FILE.read_text(encoding="utf-8")


def test_0002_creates_no_table() -> None:
    assert "CREATE TABLE" not in _code_only().upper()
