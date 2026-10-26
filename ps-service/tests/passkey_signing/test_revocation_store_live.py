"""Live proof that the signing revocation store's SQL touches only the given actor's rows.

Runs `PsycopgSigningRevocationStore` against the real passkey signing schema, applied by the
real migration runner into an isolated schema: a void and a delete scoped by subject AND issuer,
a read-only count, and a rollback when the step before COMMIT raises.

Deselected by default; select with
`uv run pytest -m postgres_live ps-service/tests/passkey_signing/test_revocation_store_live.py`.

Start a scratch Postgres:
`podman run -d --name <name> -p 54329:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`
and export `PS_STATE_POSTGRES_HOST`, `PS_STATE_POSTGRES_PORT`, `PS_STATE_POSTGRES_DATABASE` and
`PS_STATE_POSTGRES_USER` (e.g. `localhost`, `54329`, `postgres`, `postgres`). The store's
connections are opened on that server and pinned to the test's schema.

Each test works in its own uuid-named schema, dropped on teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from persistence.live_schema_support import (
    apply_passkey_directory,
    isolated_schema,
    passkey_migrations_dir,
    require_configured_state_postgres,
)
from psycopg.types.json import Jsonb

from ps_service.config import load_config
from ps_service.passkey_signing.revocation import (
    ActorSigningRevocation,
    PsycopgSigningRevocationStore,
)
from ps_service.persistence import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import LiteralString

    from psycopg import Connection
    from psycopg.rows import TupleRow

pytestmark = pytest.mark.postgres_live

SUBJECT = "owner@example.com"
ISSUER = "https://auth.example.test/application/o/ps/"
OTHER_ISSUER = "https://other.example.test/application/o/ps/"
OTHER_SUBJECT = "someone@example.com"
ACTORS = (
    (SUBJECT, ISSUER),
    (SUBJECT, OTHER_ISSUER),
    (OTHER_SUBJECT, ISSUER),
)


VOID_OUTCOME: dict[str, object] = {"error": "ask for a new approval"}


class _BeforeCommitFailedError(Exception):
    """Raised by a test's `before_commit` to force the rollback path."""


def _seed(conn: Connection[TupleRow]) -> None:
    with conn.cursor() as cur:
        for index, (subject, issuer) in enumerate(ACTORS):
            for rp in ("ps.example.test", None):
                cur.execute(
                    "INSERT INTO signing_credentials "
                    "(actor_subject, actor_issuer, credential_id, public_key, rp_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (subject, issuer, f"cred-{index}-{rp}".encode(), b"key", rp),
                )
            for state in ("pending", "expired", "signed"):
                cur.execute(
                    "INSERT INTO pending_approvals "
                    "(code_hash, tool_name, normalized_args, actor_subject, actor_issuer, nonce, "
                    "display_summary, status, outcome, expires_at) VALUES "
                    "(%s, 'merge-capabilities', '{}', %s, %s, %s, '{}', %s, %s, "
                    "CASE WHEN %s THEN now() - interval '1 minute' "
                    "ELSE now() + interval '15 minutes' END)",
                    (
                        f"code-{index}-{state}".encode(),
                        subject,
                        issuer,
                        f"nonce-{index}-{state}".encode(),
                        "signed" if state == "signed" else "pending",
                        Jsonb({"survivor_id": "a"}) if state == "signed" else None,
                        state == "expired",
                    ),
                )
    conn.commit()


def _approvals(conn: Connection[TupleRow]) -> list[tuple[str, str, str, str, object]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor_subject, actor_issuer, convert_from(code_hash, 'UTF8'), status, outcome "
            "FROM pending_approvals ORDER BY actor_subject, actor_issuer, code_hash"
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in cur.fetchall()]


def _approval_ids(conn: Connection[TupleRow], subject: str, issuer: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM pending_approvals WHERE actor_subject = %s AND actor_issuer = %s "
            "AND status = 'pending'",
            (subject, issuer),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _credential_owners(conn: Connection[TupleRow]) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor_subject, actor_issuer FROM signing_credentials "
            "ORDER BY actor_subject, actor_issuer, rp_id"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


@pytest.fixture
def schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Connection[TupleRow], str]]:
    require_configured_state_postgres()
    with isolated_schema() as (conn, name):
        apply_passkey_directory(conn, passkey_migrations_dir(), monkeypatch)
        _seed(conn)
        yield conn, name


def _store_in(schema_name: str) -> PsycopgSigningRevocationStore:
    config = load_config()

    def connect() -> Connection[TupleRow]:
        conn = connect_from_config(config)
        with conn.cursor() as cur:
            cur.execute(cast("LiteralString", f'SET search_path TO "{schema_name}"'))
        conn.commit()
        return conn

    return PsycopgSigningRevocationStore(config, connect=connect)


def test_real_sql_deletes_and_voids_only_the_given_actors_rows(
    schema: tuple[Connection[TupleRow], str],
) -> None:
    conn, name = schema
    store = _store_in(name)
    seen: list[ActorSigningRevocation] = []
    owners_pending = _approval_ids(conn, SUBJECT, ISSUER)
    before = _approvals(conn)

    result = store.revoke_for_actor(
        actor_subject=SUBJECT,
        actor_issuer=ISSUER,
        void_outcome=VOID_OUTCOME,
        before_commit=seen.append,
    )

    assert result.credentials_deleted == 2
    assert set(result.voided_approval_ids) == owners_pending
    assert len(owners_pending) == 2
    assert seen == [result]
    assert _approvals(conn) == [
        (subject, issuer, code, "signed", VOID_OUTCOME)
        if (subject, issuer) == (SUBJECT, ISSUER) and status == "pending"
        else (subject, issuer, code, status, outcome)
        for subject, issuer, code, status, outcome in before
    ]
    assert _credential_owners(conn) == [
        (SUBJECT, OTHER_ISSUER),
        (SUBJECT, OTHER_ISSUER),
        (OTHER_SUBJECT, ISSUER),
        (OTHER_SUBJECT, ISSUER),
    ]


def test_real_count_reads_only_the_given_actors_rows(
    schema: tuple[Connection[TupleRow], str],
) -> None:
    conn, name = schema

    state = _store_in(name).state_for_actor(actor_subject=SUBJECT, actor_issuer=ISSUER)

    assert state.credentials == 2
    assert len(state.pending_approval_ids) == 2
    assert len(_credential_owners(conn)) == 6


def test_real_transaction_rolls_back_when_before_commit_raises(
    schema: tuple[Connection[TupleRow], str],
) -> None:
    conn, name = schema
    before = _credential_owners(conn)
    approvals_before = _approvals(conn)

    def fail(_: ActorSigningRevocation) -> None:
        raise _BeforeCommitFailedError

    with pytest.raises(_BeforeCommitFailedError):
        _store_in(name).revoke_for_actor(
            actor_subject=SUBJECT,
            actor_issuer=ISSUER,
            void_outcome=VOID_OUTCOME,
            before_commit=fail,
        )

    assert _credential_owners(conn) == before
    assert _approvals(conn) == approvals_before
