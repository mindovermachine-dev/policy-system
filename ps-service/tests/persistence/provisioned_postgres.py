"""Shared `postgres_live` support: a scratch PS Postgres provisioned by the REAL Helm init script.

Runs `charts/policy-system/files/ps-postgres-init.sh` (the script the chart mounts into the
Postgres image) against the scratch server named by `PS_TEST_POSTGRES_SUPERUSER_DSN`, with unique
database and role names per call so live tests never collide. Used by
`test_database_isolation_live.py` and the `graph_gateway` live tests (issue #205).

Needs `PS_TEST_POSTGRES_SUPERUSER_DSN` (a superuser DSN of a scratch server, e.g.
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
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.config import ServiceConfig, load_config
from ps_service.graph_gateway.provision import ProvisioningTarget, provision
from ps_service.ingestion_runs import MIGRATIONS_DIR as INGESTION_RUNS_MIGRATIONS_DIR
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR

if TYPE_CHECKING:
    from collections.abc import Iterator

INIT_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "charts"
    / "policy-system"
    / "files"
    / "ps-postgres-init.sh"
)
INIT_SCRIPT_TIMEOUT_SECONDS = 60
# The ordinary (state-role) migrations `ps_service.main` applies at startup.
STATE_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
    MigrationSource("ingestion_runs", INGESTION_RUNS_MIGRATIONS_DIR),
]


@dataclasses.dataclass(frozen=True)
class Provisioned:
    """One provisioned PS Postgres: superuser connection params plus every role's identity."""

    host: str
    port: int
    superuser: str
    state_db: str
    state_user: str
    state_password: str
    signing_db: str
    signing_user: str
    signing_password: str
    owner_role: str

    def superuser_connect(self, dbname: str = "postgres") -> psycopg.Connection[tuple[object, ...]]:
        """Open an autocommit superuser connection to `dbname`."""
        return psycopg.connect(
            host=self.host, port=self.port, user=self.superuser, dbname=dbname, autocommit=True
        )

    def connect_as(
        self, user: str, password: str, dbname: str
    ) -> psycopg.Connection[tuple[object, ...]]:
        """Open an autocommit connection as `user`."""
        return psycopg.connect(
            host=self.host,
            port=self.port,
            user=user,
            password=password,
            dbname=dbname,
            autocommit=True,
        )

    def as_state(self, dbname: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
        """Open an autocommit connection as the `ps_state` role."""
        return self.connect_as(self.state_user, self.state_password, dbname or self.state_db)

    def as_signing(self, dbname: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
        """Open an autocommit connection as the `ps_signing` role."""
        return self.connect_as(self.signing_user, self.signing_password, dbname or self.signing_db)

    def state_config(self) -> ServiceConfig:
        """Return a `ServiceConfig` whose state Postgres is this cluster's `ps_state` role."""
        return dataclasses.replace(
            load_config(),
            state_postgres_host=self.host,
            state_postgres_port=self.port,
            state_postgres_database=self.state_db,
            state_postgres_user=self.state_user,
            state_postgres_password=self.state_password,
        )

    def provisioning_target(self) -> ProvisioningTarget:
        """Return the provisioning target: superuser as admin, `ps_state` as the app role."""
        return ProvisioningTarget(
            host=self.host,
            port=self.port,
            database=self.state_db,
            app_user=self.state_user,
            admin_user=self.superuser,
            admin_password=f"admin-pw-{uuid.uuid4().hex}",
            owner_role=self.owner_role,
        )


def superuser_params() -> dict[str, str]:
    """Return the scratch server's superuser connection parameters from the environment."""
    dsn = os.environ.get("PS_TEST_POSTGRES_SUPERUSER_DSN")
    assert dsn, "postgres_live tests require PS_TEST_POSTGRES_SUPERUSER_DSN"
    return {key: str(value) for key, value in conninfo_to_dict(dsn).items()}


def run_init_script(
    params: dict[str, str], names: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the real Helm init script against the server in `params` with `names` as its env."""
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
        ["/bin/sh", str(INIT_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=INIT_SCRIPT_TIMEOUT_SECONDS,
    )


def init_names(suffix: str) -> dict[str, str]:
    """Return unique database, role and password names for one init-script run."""
    return {
        "PS_STATE_DATABASE": f"ps_state_{suffix}",
        "PS_STATE_USER": f"ps_state_{suffix}",
        "PS_SIGNING_DATABASE": f"ps_signing_{suffix}",
        "PS_SIGNING_USER": f"ps_signing_{suffix}",
        "PS_STATE_GRAPH_OWNER_ROLE": f"ps_graph_owner_{suffix}",
        "PS_STATE_POSTGRES_PASSWORD": f"state-pw-{uuid.uuid4().hex}",
        "PS_PASSKEYSIGNING_POSTGRES_PASSWORD": f"signing-pw-{uuid.uuid4().hex}",
    }


def superuser_connection() -> psycopg.Connection[tuple[object, ...]]:
    """Open an autocommit superuser connection to the maintenance database."""
    params = superuser_params()
    return psycopg.connect(
        host=params.get("host", "127.0.0.1"),
        port=int(params.get("port", "5432")),
        user=params["user"],
        dbname="postgres",
        autocommit=True,
    )


def drop_cluster_objects(names: dict[str, str]) -> None:
    """Drop the databases and roles an init-script run created (databases first)."""
    with superuser_connection() as conn:
        for database in (names["PS_STATE_DATABASE"], names["PS_SIGNING_DATABASE"]):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
        for role in (
            names["PS_STATE_USER"],
            names["PS_SIGNING_USER"],
            names["PS_STATE_GRAPH_OWNER_ROLE"],
        ):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def create_provisioned() -> tuple[Provisioned, dict[str, str]]:
    """Run the init script with fresh names and return the cluster identity plus the names."""
    params = superuser_params()
    names = init_names(uuid.uuid4().hex[:8])
    result = run_init_script(params, names)
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
        owner_role=names["PS_STATE_GRAPH_OWNER_ROLE"],
    )
    return prov, names


def migrate_state_database(prov: Provisioned) -> None:
    """Apply the ordinary state migrations as the `ps_state` role, as service startup does."""
    with connect_from_config(prov.state_config()) as conn:
        apply_pending_migrations(conn, sources=STATE_SOURCES)


def provision_graph_log(prov: Provisioned) -> list[str]:
    """Run the privileged provisioning path (the CLI's `provision`) as the superuser admin."""
    return provision(prov.provisioning_target())


@pytest.fixture(scope="module")
def provisioned() -> Iterator[Provisioned]:
    """Provide one init-script-provisioned cluster per test module, torn down afterwards."""
    prov, names = create_provisioned()
    yield prov
    drop_cluster_objects(names)


@pytest.fixture
def fresh_provisioned() -> Iterator[Provisioned]:
    """Provide a brand-new init-script cluster per test (no state migrations applied)."""
    prov, names = create_provisioned()
    yield prov
    drop_cluster_objects(names)


@pytest.fixture(scope="module")
def provisioned_graph_log(provisioned: Provisioned) -> Provisioned:
    """Provide the module's cluster with state migrations applied and the graph log provisioned."""
    migrate_state_database(provisioned)
    provision_graph_log(provisioned)
    return provisioned
