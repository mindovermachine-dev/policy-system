"""Fast tests for `ps_service.persistence.privileged_migration_runner` (issue #205 slice 1).

The runner's database behavior (owner role creation, ownership, idempotency, the membership
refusal) is proven against a real Postgres by `tests/graph_gateway/test_provision_live.py`.
These tests cover the pure parts: role-token rendering and the shared statement splitter.
"""

from __future__ import annotations

import pytest

from ps_service.persistence import StatePostgresProvisioningError
from ps_service.persistence.migration_runner import split_statements
from ps_service.persistence.privileged_migration_runner import render_migration_sql


def test_render_migration_sql_quotes_role_names_as_identifiers() -> None:
    rendered = render_migration_sql(
        "ALTER TABLE t OWNER TO @@OWNER_ROLE@@;\nGRANT SELECT ON t TO @@APP_ROLE@@;",
        owner_role='we"ird owner',
        app_role="ps_state",
    )

    assert rendered == ('ALTER TABLE t OWNER TO "we""ird owner";\nGRANT SELECT ON t TO "ps_state";')


def test_render_migration_sql_replaces_every_occurrence_of_a_token() -> None:
    rendered = render_migration_sql(
        "@@OWNER_ROLE@@ @@OWNER_ROLE@@", owner_role="owner", app_role="app"
    )

    assert rendered == '"owner" "owner"'


def test_render_migration_sql_rejects_unknown_token() -> None:
    with pytest.raises(StatePostgresProvisioningError, match="@@SUPERUSER_ROLE@@"):
        render_migration_sql("GRANT x TO @@SUPERUSER_ROLE@@", owner_role="owner", app_role="app")


def test_render_migration_sql_leaves_sql_without_tokens_untouched() -> None:
    sql_text = "CREATE TABLE t (id int)"

    assert render_migration_sql(sql_text, owner_role="owner", app_role="app") == sql_text


def test_split_statements_keeps_a_dollar_quoted_function_body_whole() -> None:
    sql_text = (
        "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        "BEGIN\n  RETURN NEW;\nEND $$;\n"
        "CREATE TABLE t (id int);"
    )

    statements = split_statements(sql_text)

    assert len(statements) == 2
    assert "RETURN NEW;" in statements[0]
    assert statements[1] == "CREATE TABLE t (id int)"


def test_split_statements_keeps_a_tagged_dollar_quoted_body_whole() -> None:
    sql_text = "DO $body$ BEGIN PERFORM 1; PERFORM 2; END $body$;\nSELECT 1;"

    assert len(split_statements(sql_text)) == 2
