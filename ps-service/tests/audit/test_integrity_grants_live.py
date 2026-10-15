"""`postgres_live` tests: ownership and privileges of the `audit_log` schema, from the app role.

The entry surface is a SQL session as `ps_state` (the application's own database role) on a
cluster provisioned by the real Helm init script and the real provisioning path. Nothing is
mocked. Superusers (including the provisioning admin) are out of scope by design: they can
bypass any database-level control.

Deselected by default -- run with `uv run pytest -m postgres_live` (see
`persistence/provisioned_postgres.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned

pytestmark = pytest.mark.postgres_live

_AUDIT_TABLE = "audit_log.audit_events"


def _scalar(prov: Provisioned, query: str, *params: object) -> object:
    with prov.superuser_connect(prov.state_db) as conn:
        row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    assert row is not None
    return row[0]


def _column(prov: Provisioned, query: str, *params: object) -> list[str]:
    with prov.superuser_connect(prov.state_db) as conn:
        rows = conn.execute(query, params).fetchall()  # pyright: ignore[reportArgumentType]  # test-only dynamic SQL
    return [str(row[0]) for row in rows]


def _audit_log_object_owners(prov: Provisioned) -> list[tuple[str, str]]:
    """Every relation and function in `audit_log` with its owner, plus the schema itself."""
    rows = _column(
        prov,
        "SELECT kind || ':' || name || ':' || owner FROM ("
        " SELECT 'schema' AS kind, nspname::text AS name, pg_get_userbyid(nspowner)::text AS owner"
        "   FROM pg_namespace WHERE nspname = 'audit_log'"
        " UNION ALL SELECT 'relation', c.relname::text, pg_get_userbyid(c.relowner)::text"
        "   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        "   WHERE n.nspname = 'audit_log' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
        " UNION ALL SELECT 'function', p.proname::text, pg_get_userbyid(p.proowner)::text"
        "   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        "   WHERE n.nspname = 'audit_log'"
        ") objects ORDER BY 1",
    )
    return [(row.rsplit(":", 1)[0], row.rsplit(":", 1)[1]) for row in rows]


def _snapshot(prov: Provisioned) -> object:
    """Catalog facts a successful forbidden statement would change."""
    return (
        _audit_log_object_owners(prov),
        _column(
            prov,
            "SELECT attname FROM pg_attribute WHERE attrelid = 'audit_log.audit_events'::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
        ),
        _column(
            prov,
            "SELECT tgname || ':' || tgenabled::text FROM pg_trigger "
            "WHERE tgrelid = 'audit_log.audit_events'::regclass AND NOT tgisinternal "
            "ORDER BY tgname",
        ),
        _column(
            prov,
            "SELECT coalesce(relacl::text, '') FROM pg_class "
            "WHERE oid = 'audit_log.audit_events'::regclass",
        ),
        _scalar(prov, "SELECT count(*) FROM audit_log.audit_events"),
    )


def test_schema_table_and_every_function_are_owned_by_the_owner_role_and_the_app_role_is_no_member(
    provisioned_state: Provisioned,
) -> None:
    owners = _audit_log_object_owners(provisioned_state)

    assert {kind for kind, _ in owners} >= {"schema:audit_log", "relation:audit_events"}
    assert {owner for _, owner in owners} == {provisioned_state.owner_role}
    assert (
        _scalar(
            provisioned_state,
            "SELECT pg_has_role(%s, %s, 'MEMBER')",
            provisioned_state.state_user,
            provisioned_state.owner_role,
        )
        is False
    )
    assert (
        _scalar(
            provisioned_state,
            "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls "
            "FROM pg_roles WHERE rolname = %s",
            provisioned_state.state_user,
        )
        is False
    )


@pytest.mark.parametrize(
    "statement",
    [
        f"DROP TABLE {_AUDIT_TABLE}",
        f"ALTER TABLE {_AUDIT_TABLE} ADD COLUMN x int",
        f"ALTER TABLE {_AUDIT_TABLE} DISABLE TRIGGER ALL",
        f"ALTER TABLE {_AUDIT_TABLE} DISABLE TRIGGER USER",
        f"TRUNCATE {_AUDIT_TABLE}",
        "DROP SCHEMA audit_log CASCADE",
        f"ALTER TABLE {_AUDIT_TABLE} SET SCHEMA public",
        (
            f"CREATE TRIGGER x_forged BEFORE INSERT ON {_AUDIT_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION pg_catalog.suppress_redundant_updates_trigger()"
        ),
        f"ALTER TABLE {_AUDIT_TABLE} OWNER TO CURRENT_USER",
        "CREATE TABLE audit_log.x (a int)",
        "CREATE FUNCTION audit_log.x() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        f"ALTER TABLE {_AUDIT_TABLE} RENAME TO audit_events_old",
        f"CREATE INDEX x_idx ON {_AUDIT_TABLE} (action)",
        "ALTER SCHEMA audit_log RENAME TO audit_log_old",
    ],
)
def test_the_app_role_cannot_alter_drop_or_re_grant_the_audit_schema_or_table(
    provisioned_state: Provisioned, statement: str
) -> None:
    before = _snapshot(provisioned_state)

    with (
        provisioned_state.as_state() as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)  # pyright: ignore[reportCallIssue, reportArgumentType]  # test-only dynamic SQL

    assert _snapshot(provisioned_state) == before


@pytest.mark.parametrize(
    "statement",
    [
        f"GRANT ALL ON {_AUDIT_TABLE} TO PUBLIC",
        f"GRANT UPDATE, DELETE, TRUNCATE ON {_AUDIT_TABLE} TO CURRENT_USER",
    ],
)
def test_a_grant_by_the_app_role_changes_no_privilege_because_it_holds_no_grant_option(
    provisioned_state: Provisioned, statement: str
) -> None:
    # Postgres answers a GRANT by a non-owner without a grant option with a warning, not an error.
    before = _snapshot(provisioned_state)

    with provisioned_state.as_state() as conn:
        conn.execute(statement)  # pyright: ignore[reportCallIssue, reportArgumentType]  # test-only dynamic SQL

    assert _snapshot(provisioned_state) == before


def test_select_on_the_audit_table_is_held_only_by_the_app_role_and_the_owner_role(
    provisioned_state: Provisioned,
) -> None:
    prov = provisioned_state
    expected = {prov.state_user, prov.owner_role}
    candidates = {prov.state_user, prov.owner_role, prov.signing_user}

    holders = {
        role
        for role in _column(
            prov,
            "SELECT rolname FROM pg_roles WHERE NOT rolsuper AND rolname !~ '^pg_'",
        )
        if _scalar(
            prov,
            "SELECT has_table_privilege(%s, 'audit_log.audit_events', 'SELECT') "
            "OR has_any_column_privilege(%s, 'audit_log.audit_events', 'SELECT')",
            role,
            role,
        )
    }

    assert candidates & holders == expected
    # Any other non-superuser role holding SELECT must be one this cluster's two principals
    # reach by membership; the shared scratch cluster hosts unrelated databases and roles.
    for role in holders - expected:
        assert _scalar(
            prov,
            "SELECT pg_has_role(%s, %s, 'MEMBER') OR pg_has_role(%s, %s, 'MEMBER')",
            role,
            prov.state_user,
            role,
            prov.owner_role,
        ), f"unexpected SELECT holder {role}"


def test_no_one_but_the_app_role_and_owner_holds_any_other_privilege_and_public_holds_none(
    provisioned_state: Provisioned,
) -> None:
    prov = provisioned_state

    grants = _column(
        prov,
        "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END "
        "|| ':' || a.privilege_type "
        "FROM pg_class c, aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a "
        "WHERE c.oid = 'audit_log.audit_events'::regclass ORDER BY 1",
    )

    # The owner holds its own default privileges (the set differs by Postgres version).
    assert [g for g in grants if not g.startswith(f"{prov.owner_role}:")] == [
        f"{prov.state_user}:INSERT",
        f"{prov.state_user}:SELECT",
    ], grants
    assert not any(grant.startswith("PUBLIC:") for grant in grants)
    schema_grants = _column(
        prov,
        "SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END "
        "|| ':' || a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'audit_log' ORDER BY 1",
    )
    assert not any(grant.startswith("PUBLIC:") for grant in schema_grants)
    assert f"{prov.state_user}:USAGE" in schema_grants
    assert f"{prov.state_user}:CREATE" not in schema_grants


def test_the_signing_role_and_pg_data_roles_give_no_access_to_the_audit_table(
    provisioned_state: Provisioned,
) -> None:
    prov = provisioned_state

    assert (
        _scalar(
            prov,
            "SELECT has_schema_privilege(%s, 'audit_log', 'USAGE') "
            "OR has_table_privilege(%s, 'audit_log.audit_events', 'SELECT')",
            prov.signing_user,
            prov.signing_user,
        )
        is False
    )
    for data_role in ("pg_read_all_data", "pg_write_all_data"):
        assert (
            _scalar(prov, "SELECT pg_has_role(%s, %s, 'MEMBER')", prov.state_user, data_role)
            is False
        )


def test_no_function_in_the_audit_schema_is_executable_by_public_or_the_app_role_unless_allowed(
    provisioned_state: Provisioned,
) -> None:
    prov = provisioned_state
    allowed_to_app_role = {"verify_chain"}

    executable = _column(
        prov,
        "SELECT p.proname || ':' || CASE a.grantee WHEN 0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE n.nspname = 'audit_log' AND a.privilege_type = 'EXECUTE'",
    )

    for entry in executable:
        function, grantee = entry.split(":")
        assert grantee != "PUBLIC", entry
        assert grantee == prov.owner_role or (
            grantee == prov.state_user and function in allowed_to_app_role
        ), entry
