"""`postgres_live` tests: the canonical form `ps-audit-v1` and the genesis hash are DB-defined.

The entry surface is a SQL session calling the same functions every writer path uses (the chain
trigger), on a cluster provisioned by the real Helm init script and the real provisioning path.
EXECUTE on them is revoked from the application role, so the tests call them as the superuser.

The oracle below implements the format document independently in Python. It takes the text form
of a `jsonb` value from the database (`jsonb::text`), so the normalisation of `jsonb` itself is
not independently checked here; everything else (version tag, length prefixes, UTC microsecond
timestamps, hashing) is.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.types.json import Jsonb

from audit._integrity_oracle import GENESIS, canonical_v1
from audit._integrity_oracle import chain_hash as _chain_hash

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_GENESIS = GENESIS

_CANONICAL_SQL = "SELECT audit_log.canonical_row(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"

_Row = tuple[int, uuid.UUID, datetime, str, str, str, str, str, str, object]


def _canonical_v1(row: _Row, details_text: str) -> bytes:
    """The oracle's bytes for a test row tuple."""
    chain_seq, row_id, occurred_at, *strings, _details = row
    return canonical_v1(
        chain_seq=chain_seq,
        row_id=row_id,
        occurred_at=occurred_at,
        strings=tuple(strings),  # pyright: ignore[reportArgumentType]
        details_text=details_text,
    )


def _row(
    *,
    seq: int = 1,
    occurred_at: datetime | None = None,
    strings: tuple[str, ...] = ("subject", "https://issuer.example.com/", "act.ion", "type", "rid"),
    outcome: str = "applied",
    details: object = None,
) -> _Row:
    subject, issuer, action, rtype, rid = strings
    return (
        seq,
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        occurred_at or datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=UTC),
        subject,
        issuer,
        action,
        rtype,
        rid,
        outcome,
        {} if details is None else details,
    )


def _params(row: _Row) -> tuple[object, ...]:
    return (*row[:-1], Jsonb(row[-1]))


_GOLDEN_ROWS: dict[str, _Row] = {
    "ascii": _row(details={"a": 1, "b": [1, 2, {"c": None}]}),
    "multibyte": _row(
        strings=("subéject-日本", "https://ü.example/", "a.☃", "t", "r\U0001f600"),
        details={"kü": "v☃", "z": "\U0001f600"},
    ),
    "empty-fields": _row(strings=("", "", "", "", ""), details={}),
    "nested-unicode-numbers": _row(
        details={"n": {"deep": ["å", 1.5e300, 0.1, -0.0, 12345678901234567890]}, "b": True},
    ),
    "microsecond-before-epoch": _row(
        seq=2, occurred_at=datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    ),
    "microsecond-after-epoch": _row(
        seq=3, occurred_at=datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    ),
    "offset-input": _row(
        seq=10**12,
        occurred_at=datetime(
            2024, 5, 6, 12, 38, 9, 5, tzinfo=timezone(timedelta(hours=5, minutes=30))
        ),
    ),
}


@pytest.fixture(scope="module")
def conn(provisioned_state: Provisioned) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A superuser connection to the provisioned state database (the app has no EXECUTE)."""
    with provisioned_state.superuser_connect(provisioned_state.state_db) as connection:
        yield connection


def _details_text(conn: psycopg.Connection[tuple[object, ...]], details: object) -> str:
    row = conn.execute("SELECT %s::jsonb::text", (json.dumps(details),)).fetchone()  # pyright: ignore[reportArgumentType]
    assert row is not None
    return str(row[0])


def _canonical(conn: psycopg.Connection[tuple[object, ...]], row: _Row) -> bytes:
    fetched = conn.execute(_CANONICAL_SQL, _params(row)).fetchone()  # pyright: ignore[reportArgumentType]
    assert fetched is not None
    return bytes(fetched[0])  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("name", sorted(_GOLDEN_ROWS))
def test_the_sql_canonical_form_and_hash_equal_the_independent_oracle(
    conn: psycopg.Connection[tuple[object, ...]], name: str
) -> None:
    row = _GOLDEN_ROWS[name]
    expected = _canonical_v1(row, _details_text(conn, row[-1]))

    actual = _canonical(conn, row)
    hashed = conn.execute("SELECT audit_log.chain_hash(%s, %s)", (_GENESIS, actual)).fetchone()

    assert actual == expected
    assert hashed == (_chain_hash(_GENESIS, expected),)


_SESSION_SETTINGS = (
    ("TimeZone", "Asia/Kolkata"),
    ("TimeZone", "America/St_Johns"),
    ("DateStyle", "SQL, DMY"),
    ("DateStyle", "German, DMY"),
    ("IntervalStyle", "iso_8601"),
    ("extra_float_digits", "-3"),
    ("lc_messages", "C"),
    ("bytea_output", "escape"),
    ("standard_conforming_strings", "off"),
    ("client_encoding", "LATIN1"),
)


def test_the_hash_is_identical_under_unusual_session_settings(
    provisioned_state: Provisioned,
) -> None:
    row = _GOLDEN_ROWS["nested-unicode-numbers"]
    with provisioned_state.superuser_connect(provisioned_state.state_db) as plain:
        reference = _canonical(plain, row)

    for setting, value in _SESSION_SETTINGS:
        with provisioned_state.superuser_connect(provisioned_state.state_db) as session:
            session.execute("SELECT set_config(%s, %s, false)", (setting, value))
            # Compare in the database, so `bytea_output` and `client_encoding` cannot hide a diff.
            equal = session.execute(
                "SELECT audit_log.chain_hash(%s, audit_log.canonical_row("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)) = %s",
                (_GENESIS, *_params(row), _chain_hash(_GENESIS, reference)),
            ).fetchone()
        assert equal == (True,), f"{setting}={value}"


def test_two_rows_that_differ_only_in_a_field_boundary_hash_differently(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    first = _row(strings=("ab", "c", "act", "t", "r"))
    second = _row(strings=("a", "bc", "act", "t", "r"))

    assert _canonical(conn, first) != _canonical(conn, second)


def test_changing_any_single_field_changes_the_canonical_bytes(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    base = _row()
    variants = [
        _row(seq=2),
        _row(occurred_at=datetime(2024, 5, 6, 7, 8, 9, 123457, tzinfo=UTC)),
        _row(strings=("other", "https://issuer.example.com/", "act.ion", "type", "rid")),
        _row(outcome="rejected"),
        _row(details={"x": 1}),
    ]

    forms = {_canonical(conn, row) for row in [base, *variants]}

    assert len(forms) == 1 + len(variants)


def test_a_null_input_or_an_infinite_timestamp_raises_instead_of_hashing(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    null_subject = list(_params(_row()))
    null_subject[3] = None
    with pytest.raises(psycopg.errors.NullValueNotAllowed):
        conn.execute(_CANONICAL_SQL, null_subject)  # pyright: ignore[reportArgumentType]

    infinite = list(_params(_row()))
    infinite[2] = "infinity"
    with pytest.raises(psycopg.errors.DatabaseError):
        conn.execute(
            "SELECT audit_log.canonical_row(%s, %s, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s)",
            infinite,  # pyright: ignore[reportArgumentType]
        )


def test_the_genesis_hash_is_64_zero_characters_and_not_null(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    assert conn.execute("SELECT audit_log.genesis_hash()").fetchone() == (_GENESIS,)


def test_the_hash_functions_are_immutable_and_pin_their_search_path(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = conn.execute(
        "SELECT proname, provolatile, proconfig FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'audit_log' AND proname IN "
        "('genesis_hash', 'canonical_field', 'canonical_row', 'chain_hash') ORDER BY proname"
    ).fetchall()

    names = {row[0] for row in rows}
    assert names == {"genesis_hash", "canonical_field", "canonical_row", "chain_hash"}
    for name, volatility, config in rows:
        assert volatility == "i", name
        assert config == ["search_path=pg_catalog, pg_temp"], name


def test_the_chain_columns_exist_and_are_typed(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = conn.execute(  # pyright: ignore[reportCallIssue, reportArgumentType]
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'audit_log' AND table_name = 'audit_events' "
        "AND column_name IN ('chain_seq', 'prev_row_hash', 'row_hash')"
    ).fetchall()
    columns = {str(name): str(kind) for name, kind in rows}

    assert columns == {"chain_seq": "bigint", "prev_row_hash": "text", "row_hash": "text"}
