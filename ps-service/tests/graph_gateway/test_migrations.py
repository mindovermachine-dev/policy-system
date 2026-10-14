"""Fast checks of the `graph_gateway` migration files and startup wiring.

The live application of the migrations is covered by `test_provision_live.py`.
"""

from __future__ import annotations

import re

import pytest

from ps_service import main as main_module
from ps_service.graph_gateway import MIGRATIONS_DIR

_FILES = ("0001_graph_mutation_log.sql", "0002_group_swap_set.sql")
_SWAP_SET_FILE = MIGRATIONS_DIR / "0002_group_swap_set.sql"


def _code(name: str) -> str:
    return re.sub(r"--[^\n]*", "", (MIGRATIONS_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _FILES)
def test_graph_gateway_migration_has_no_semicolon_in_comments(name: str) -> None:
    comments = re.findall(r"--[^\n]*", (MIGRATIONS_DIR / name).read_text(encoding="utf-8"))

    assert comments
    assert [comment for comment in comments if ";" in comment] == []


def test_graph_gateway_migrations_are_the_two_append_only_files_in_order() -> None:
    assert [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))] == list(_FILES)
    for name in _FILES:
        text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        assert "append-only from the first deployment onward" in text, name


@pytest.mark.parametrize("name", _FILES)
def test_graph_gateway_migration_names_roles_only_through_render_tokens(name: str) -> None:
    code = _code(name)

    assert re.findall(r"@@(\w+)@@", code) != []
    assert set(re.findall(r"@@(\w+)@@", code)) <= {"OWNER_ROLE", "APP_ROLE"}
    assert "@@OWNER_ROLE@@" in code


def test_the_first_migration_names_both_role_tokens() -> None:
    assert "@@APP_ROLE@@" in _code(_FILES[0])


def test_the_swap_set_migration_adds_one_nullable_column_and_a_partial_index_as_the_owner() -> None:
    code = _code(_SWAP_SET_FILE.name)
    statements = [statement.strip() for statement in code.split(";") if statement.strip()]

    assert statements == [
        "SET LOCAL ROLE @@OWNER_ROLE@@",
        "ALTER TABLE graph_log.groups ADD COLUMN swap_set_id uuid",
        (
            "CREATE INDEX groups_swap_set_idx ON graph_log.groups (swap_set_id)"
            " WHERE swap_set_id IS NOT NULL"
        ),
        "RESET ROLE",
    ]


def test_the_swap_set_migration_resets_the_role_before_the_runner_records_it() -> None:
    code = _code(_SWAP_SET_FILE.name)

    assert code.strip().endswith("RESET ROLE;")
    assert code.index("SET LOCAL ROLE") < code.index("ALTER TABLE") < code.index("RESET ROLE")
    assert "NOT NULL" not in code.split("ADD COLUMN")[1].split(";")[0]  # nullable, additive


def test_state_startup_sources_do_not_include_graph_gateway() -> None:
    """The ordinary startup runner must never create the owner-protected tables itself."""
    components = [source.component for source in main_module.ORDINARY_STATE_MIGRATION_SOURCES]

    assert components
    assert "graph_gateway" not in components
