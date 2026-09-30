"""`postgres_live` tests for the PS Postgres init script's database isolation (issue #130 S5).

Runs the SAME `charts/policy-system/files/ps-postgres-init.sh` the Helm chart mounts into
the Postgres image (via `templates/ps-postgres-init-configmap.yaml`) against a scratch
server, with unique database/role names per session, then proves AC-BI-016 with real
connections: neither role can connect to the other's database, a mistakenly re-granted
`CONNECT` still leaves the other database's tables unreadable and `public` unwritable, and
`PUBLIC` holds neither `CONNECT` nor `CREATE` on `public` in either database. It also proves
the least-privilege claim: migrations and the catalog-source tool body work as the `ps_state`
role alone.

Deselected by default -- run with `uv run pytest -m postgres_live`. Needs
`PS_TEST_POSTGRES_SUPERUSER_DSN` (a superuser DSN of a scratch server, e.g.
`postgresql://postgres@127.0.0.1:54329/postgres`) and `psql` on `PATH`.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import ServiceConfig, load_config
from ps_service.curated_source.store import get_override, reset_override, set_override
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.postgres_live

_INIT_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "charts"
    / "policy-system"
    / "files"
    / "ps-postgres-init.sh"
)
_INIT_SCRIPT_TIMEOUT_SECONDS = 60
_STATE_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
]
_ISSUER = "https://issuer.example.com/"


@dataclasses.dataclass(frozen=True)
class Provisioned:
    """One provisioned PS Postgres: superuser connection params plus both roles' identities."""

    host: str
    port: int
    superuser: str
    state_db: str
    state_user: str
    state_password: str
    signing_db: str
    signing_user: str
    signing_password: str

    def superuser_connect(self, dbname: str = "postgres") -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(
            host=self.host, port=self.port, user=self.superuser, dbname=dbname, autocommit=True
        )

    def connect_as(
        self, user: str, password: str, dbname: str
    ) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            user=user,
            password=password,
            dbname=dbname,
            autocommit=True,
        )

    def as_state(self, dbname: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
        return self.connect_as(self.state_user, self.state_password, dbname or self.state_db)

    def as_signing(self, dbname: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
        return self.connect_as(self.signing_user, self.signing_password, dbname or self.signing_db)


def _superuser_params() -> dict[str, str]:
    dsn = os.environ.get("PS_TEST_POSTGRES_SUPERUSER_DSN")
    assert dsn, "postgres_live isolation tests require PS_TEST_POSTGRES_SUPERUSER_DSN"
    return {key: str(value) for key, value in conninfo_to_dict(dsn).items()}


def _run_init_script(
    params: dict[str, str], names: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PGHOST": params.get("host", "127.0.0.1"),
        "PGPORT": params.get("port", "5432"),
        "POSTGRES_USER": params["user"],
        **names,
    }
    if "password" in params:
        env["PGPASSWORD"] = params["password"]
    return subprocess.run(  # noqa: S603  # fixed argv, repo-owned script, no shell
        ["/bin/sh", str(_INIT_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_INIT_SCRIPT_TIMEOUT_SECONDS,
    )


def _init_names(suffix: str) -> dict[str, str]:
    return {
        "PS_STATE_DATABASE": f"ps_state_{suffix}",
        "PS_STATE_USER": f"ps_state_{suffix}",
        "PS_SIGNING_DATABASE": f"ps_signing_{suffix}",
        "PS_SIGNING_USER": f"ps_signing_{suffix}",
        "PS_STATE_POSTGRES_PASSWORD": f"state-pw-{uuid.uuid4().hex}",
        "PS_PASSKEYSIGNING_POSTGRES_PASSWORD": f"signing-pw-{uuid.uuid4().hex}",
    }


@pytest.fixture(scope="module")
def provisioned() -> Iterator[Provisioned]:
    params = _superuser_params()
    names = _init_names(uuid.uuid4().hex[:8])
    result = _run_init_script(params, names)
    assert result.returncode == 0, result.stderr
    prov = Provisioned(
        host=params.get("host", "127.0.0.1"),
        port=int(params.get("port", "5432")),
        superuser=params["user"],
        state_db=names["PS_STATE_DATABASE"],
        state_user=names["PS_STATE_USER"],
        state_password=names["PS_STATE_POSTGRES_PASSWORD"],
        signing_db=names["PS_SIGNING_DATABASE"],
        signing_user=names["PS_SIGNING_USER"],
        signing_password=names["PS_PASSKEYSIGNING_POSTGRES_PASSWORD"],
    )
    yield prov
    with prov.superuser_connect() as conn:
        for database in (prov.state_db, prov.signing_db):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
        for role in (prov.state_user, prov.signing_user):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_init_script_output_never_contains_a_password() -> None:
    names = _init_names(uuid.uuid4().hex[:8])

    result = _run_init_script(_superuser_params(), names)

    try:
        assert result.returncode == 0, result.stderr
        for secret in (
            names["PS_STATE_POSTGRES_PASSWORD"],
            names["PS_PASSKEYSIGNING_POSTGRES_PASSWORD"],
        ):
            assert secret not in result.stdout
            assert secret not in result.stderr
    finally:
        with _superuser_connection() as conn:
            for database in (names["PS_STATE_DATABASE"], names["PS_SIGNING_DATABASE"]):
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
            for role in (names["PS_STATE_USER"], names["PS_SIGNING_USER"]):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _superuser_connection() -> psycopg.Connection[tuple[object, ...]]:
    params = _superuser_params()
    return psycopg.connect(
        host=params.get("host", "127.0.0.1"),
        port=int(params.get("port", "5432")),
        user=params["user"],
        dbname="postgres",
        autocommit=True,
    )


def test_each_role_can_connect_and_create_in_its_own_database(provisioned: Provisioned) -> None:
    with provisioned.as_state() as conn:
        conn.execute("CREATE TABLE own_state_probe (id int)")
    with provisioned.as_signing() as conn:
        conn.execute("CREATE TABLE own_signing_probe (id int)")


def test_signing_role_cannot_connect_to_state_database(provisioned: Provisioned) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        provisioned.as_signing(provisioned.state_db).close()


def test_state_role_cannot_connect_to_signing_database(provisioned: Provisioned) -> None:
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        provisioned.as_state(provisioned.signing_db).close()


def test_role_cannot_read_or_create_in_other_database_even_when_connect_is_regranted(
    provisioned: Provisioned,
) -> None:
    with provisioned.as_state() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS victim_state_table (id int)")
        conn.execute("INSERT INTO victim_state_table VALUES (1)")
    with provisioned.superuser_connect() as admin:
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(provisioned.state_db), sql.Identifier(provisioned.signing_user)
            )
        )
    try:
        with provisioned.as_signing(provisioned.state_db) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM victim_state_table")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE intruder_table (id int)")
    finally:
        with provisioned.superuser_connect() as admin:
            admin.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(provisioned.state_db), sql.Identifier(provisioned.signing_user)
                )
            )


def test_public_has_no_connect_and_no_create_on_public_schema_in_either_database(
    provisioned: Provisioned,
) -> None:
    with provisioned.superuser_connect() as conn:
        for database in (provisioned.state_db, provisioned.signing_db):
            row = conn.execute(
                "SELECT has_database_privilege('public', %s, 'CONNECT')", (database,)
            ).fetchone()
            assert row == (False,), f"PUBLIC still has CONNECT on {database}"
    for database in (provisioned.state_db, provisioned.signing_db):
        with provisioned.superuser_connect(database) as conn:
            row = conn.execute(
                "SELECT has_schema_privilege('public', 'public', 'CREATE')"
            ).fetchone()
            assert row == (False,), f"PUBLIC still has CREATE on schema public in {database}"


def test_init_script_rerun_fails_loudly_and_never_silently_succeeds(
    provisioned: Provisioned,
) -> None:
    names = {
        "PS_STATE_DATABASE": provisioned.state_db,
        "PS_STATE_USER": provisioned.state_user,
        "PS_SIGNING_DATABASE": provisioned.signing_db,
        "PS_SIGNING_USER": provisioned.signing_user,
        "PS_STATE_POSTGRES_PASSWORD": provisioned.state_password,
        "PS_PASSKEYSIGNING_POSTGRES_PASSWORD": provisioned.signing_password,
    }

    result = _run_init_script(_superuser_params(), names)

    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_migrations_and_catalog_tool_work_as_the_least_privilege_state_role(
    provisioned: Provisioned,
) -> None:
    config: ServiceConfig = dataclasses.replace(
        load_config(),
        state_postgres_host=provisioned.host,
        state_postgres_port=provisioned.port,
        state_postgres_database=provisioned.state_db,
        state_postgres_user=provisioned.state_user,
        state_postgres_password=provisioned.state_password,
    )
    actor = ("least-privilege-actor", _ISSUER)
    store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))

    with connect_from_config(config) as conn:
        apply_pending_migrations(conn, sources=_STATE_SOURCES)
    set_override(store, "https://example.com/least-privilege", actor=actor)

    assert get_override(store) == "https://example.com/least-privilege"
    reset_override(store, actor=actor)
    assert get_override(store) is None
    with provisioned.as_state() as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_events WHERE actor_subject = %s", ("least-privilege-actor",)
        ).fetchone()
    assert row is not None
    assert row[0] == 2
