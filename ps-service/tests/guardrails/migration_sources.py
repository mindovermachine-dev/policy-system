"""Discovery of the PS service's SQL migration directories, shared by the migration guards.

A migration directory is any directory under `ps-service/src/ps_service` holding at least one
`NNNN_*.sql` file (four digits, an underscore, a name). Discovery looks at file names only, so a
new component or a new directory name is found without any list to update. The table parser
reports which `<database>.<schema>.<table>` names the migrations leave in place. Reads the file
tree only; writes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_SOURCE = REPO_ROOT / "ps-service" / "src" / "ps_service"

BOOKKEEPING_TABLES = frozenset(
    {"ps_state.public.ps_schema_migrations", "ps_signing.public.schema_migrations"}
)
"""The migration runners' own record of applied files, one per database."""

_MIGRATION_FILE = re.compile(r"^\d{4}_.+\.sql$")
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)", re.IGNORECASE
)
_MOVE_TABLE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)\s+SET\s+SCHEMA\s+(\w+)",
    re.IGNORECASE,
)


def migration_files(directory: Path) -> list[Path]:
    """Return the `NNNN_*.sql` files directly in `directory`, in apply (name) order."""
    return sorted(
        path for path in directory.iterdir() if path.is_file() and _MIGRATION_FILE.match(path.name)
    )


def migration_directories(source_root: Path = SERVICE_SOURCE) -> list[Path]:
    """Return every directory under `source_root` holding a `NNNN_*.sql` file, sorted."""
    return sorted(
        {path.parent for path in source_root.rglob("*.sql") if _MIGRATION_FILE.match(path.name)}
    )


def tables_created_by_migrations(databases: Mapping[Path, str]) -> set[str]:
    """Return `<database>.<schema>.<table>` left in place by every file of every directory.

    `databases` maps each absolute migration directory to the database its files run in. Every
    `CREATE TABLE` is collected first and every `ALTER TABLE ... SET SCHEMA` is applied after,
    so the result does not depend on the order the directories are given in. SQL comments are
    ignored. The migration runners' own tables are not included (see `BOOKKEEPING_TABLES`).

    Args:
        databases: Absolute migration directory to database name (`ps_state` or `ps_signing`).

    Returns:
        The qualified names of the tables the migrations create, after schema moves.
    """
    tables: set[str] = set()
    moves: list[tuple[str, str]] = []
    for directory, database in databases.items():
        for path in migration_files(directory):
            text = re.sub(r"--[^\n]*", "", path.read_text())
            for statement in text.split(";"):
                if created := _CREATE_TABLE.search(statement):
                    tables.add(f"{database}.{created.group(1) or 'public'}.{created.group(2)}")
                if moved := _MOVE_TABLE.search(statement):
                    source = f"{database}.{moved.group(1) or 'public'}.{moved.group(2)}"
                    moves.append((source, f"{database}.{moved.group(3)}.{moved.group(2)}"))
    for source, target in moves:
        if source in tables:
            tables.discard(source)
            tables.add(target)
    return tables
