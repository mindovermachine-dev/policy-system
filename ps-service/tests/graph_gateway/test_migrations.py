"""Fast checks of the `graph_gateway` migration files and startup wiring (issue #205 slice 1).

The live application of the migration is covered by `test_provision_live.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ps_service import main as main_module
from ps_service.graph_gateway import MIGRATIONS_DIR

_FILE = MIGRATIONS_DIR / "0001_graph_mutation_log.sql"


def test_graph_gateway_migration_has_no_semicolon_in_comments() -> None:
    comments = re.findall(r"--[^\n]*", _FILE.read_text(encoding="utf-8"))

    assert comments
    assert [comment for comment in comments if ";" in comment] == []


def test_graph_gateway_migration_is_append_only_named_0001() -> None:
    assert [path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql"))] == [
        "0001_graph_mutation_log.sql"
    ]
    assert "append-only from the first deployment onward" in _FILE.read_text(encoding="utf-8")


def test_graph_gateway_migration_names_roles_only_through_render_tokens() -> None:
    code = re.sub(r"--[^\n]*", "", _FILE.read_text(encoding="utf-8"))

    assert "@@OWNER_ROLE@@" in code
    assert "@@APP_ROLE@@" in code
    assert re.findall(r"@@(\w+)@@", code) != []
    assert set(re.findall(r"@@(\w+)@@", code)) <= {"OWNER_ROLE", "APP_ROLE"}


def test_state_startup_sources_do_not_include_graph_gateway() -> None:
    """The ordinary startup runner must never create the owner-protected tables itself."""
    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    startup = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_state_migrations_at_startup"
    )
    components = [
        call.args[0].value
        for call in ast.walk(startup)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "MigrationSource"
        and isinstance(call.args[0], ast.Constant)
    ]

    assert components
    assert "graph_gateway" not in components
