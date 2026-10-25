"""Migration coverage guard: every SQL migration directory names the live tests that prove it.

Every directory of `NNNN_*.sql` files under `ps-service/src/ps_service` needs an entry in
`guardrails/migration_registry.py` naming a `postgres_live` test that applies it to an empty
database and one that applies its newest migration on top of the previous release's schema. The
guard reads the registered test files with `ast` (it never imports or runs them), so it proves the
tests exist, are collected and are marked, while the live run itself proves the SQL. An entry also
records the directory's newest file, so adding a migration fails here until the entry names an
upgrade test for it.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import fields, replace
from typing import TYPE_CHECKING

import pytest

from guardrails.migration_registry import REGISTRY, MigrationCoverage
from guardrails.migration_sources import (
    REPO_ROOT,
    SERVICE_SOURCE,
    migration_directories,
    migration_files,
)
from ps_service.state_migrations import (
    AUDIT_INTEGRITY_MIGRATION_SOURCE,
    GRAPH_GATEWAY_MIGRATION_SOURCE,
    ORDINARY_STATE_MIGRATION_SOURCES,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

STATE_DIRECTORIES = frozenset(
    source.directory.resolve()
    for source in (
        *ORDINARY_STATE_MIGRATION_SOURCES,
        GRAPH_GATEWAY_MIGRATION_SOURCE,
        AUDIT_INTEGRITY_MIGRATION_SOURCE,
    )
)
"""Directories applied to `ps_state`; every other migration directory runs in `ps_signing`."""

_COLLECTED_FILE = re.compile(r"^test_.*\.py$")
_NEVER_RUNS = ("skip", "skipif", "xfail")
_SELF_SKIP_CALLS = ("skip", "importorskip")
_TEST_FIX = "point {field} at a module-level postgres_live test that runs"


def _mark_name(node: ast.expr) -> str | None:
    """Return `x` for `<...>.mark.x` or `<...>.mark.x(...)`, else None."""
    target = node.func if isinstance(node, ast.Call) else node
    if not isinstance(target, ast.Attribute):
        return None
    owner = target.value
    if (isinstance(owner, ast.Attribute) and owner.attr == "mark") or (
        isinstance(owner, ast.Name) and owner.id == "mark"
    ):
        return target.attr
    return None


def _module_marks(tree: ast.Module) -> list[str]:
    """Return the mark names a module-level `pytestmark` applies to every test in the file."""
    marks: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            named = any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in statement.targets
            )
            value: ast.expr | None = statement.value if named else None
        elif isinstance(statement, ast.AnnAssign):
            named = isinstance(statement.target, ast.Name) and statement.target.id == "pytestmark"
            value = statement.value if named else None
        else:
            value = None
        if value is None:
            continue
        elements = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
        marks.extend(name for element in elements if (name := _mark_name(element)) is not None)
    return marks


def _skips_itself(tree: ast.Module) -> bool:
    """Return whether a top-level statement calls `<...>.skip(...)` or `<...>.importorskip(...)`."""
    for statement in tree.body:
        if not isinstance(statement, ast.Expr | ast.Assign | ast.AnnAssign):
            continue
        call = statement.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _SELF_SKIP_CALLS
        ):
            return True
    return False


def _resolve(node_id: str, repo_root: Path) -> str | None:
    """Return why `node_id` cannot prove a migration, or None when it is a runnable live test.

    The node id is `<repo-relative path>::<function>` with an optional `[<case id>]`. The test
    must be a module-level function in a file pytest collects, carry the `postgres_live` mark
    (decorator or module `pytestmark`), and carry no mark or module-level call that keeps it from
    running. Marks given per case through `pytest.param` do not count.
    """
    path_text, _, qualified = node_id.partition("::")
    name, has_case, case = qualified.partition("[")
    path = repo_root / path_text
    if "::" in name or not name.startswith("test"):
        return "is not a module-level test function"
    if not path.is_file():
        return "test file does not exist"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        ),
        None,
    )
    if function is None:
        return f"has no module-level test function {name}"
    decorator_marks = [
        mark for decorator in function.decorator_list if (mark := _mark_name(decorator))
    ]
    if has_case and "parametrize" not in decorator_marks:
        return f"names case [{case.removesuffix(']')}] but {name} is not parametrized"
    marks = [*decorator_marks, *_module_marks(tree)]
    if "postgres_live" not in marks:
        return "is not marked postgres_live"
    if not _COLLECTED_FILE.match(path.name):
        return "is in a file pytest does not collect (name it test_*.py)"
    if never_runs := next((mark for mark in marks if mark in _NEVER_RUNS), None):
        return f"is marked {never_runs}, so it never proves the migration"
    if _skips_itself(tree):
        return "is in a module that skips itself"
    return None


def _display(directory: Path, repo_root: Path) -> str:
    """Return `directory` relative to `repo_root` when it is inside it, else absolute."""
    resolved = directory.resolve()
    root = repo_root.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)


def _entry_problems(
    entry: MigrationCoverage, repo_root: Path, state_directories: frozenset[Path]
) -> list[str]:
    """Return what is wrong with one registry entry, each problem naming its directory."""
    directory = repo_root / entry.directory
    key = entry.directory
    if not directory.is_dir():
        return [f"{key}: is registered but does not exist. How to fix: remove or correct the entry"]
    files = [path.name for path in migration_files(directory)]
    if not files:
        empty = f"{key}: is registered but holds no NNNN_*.sql migrations"
        return [f"{empty}. How to fix: remove or correct the entry"]
    problems: list[str] = []
    if files[-1] != entry.latest_migration:
        problems.append(
            f"{key}: newest migration is {files[-1]}, the entry records {entry.latest_migration}. "
            "How to fix: update latest_migration and register its N-1→N upgrade test"
        )
    if len(files) >= 2 and entry.empty_apply_test == entry.upgrade_test:
        problems.append(
            f"{key}: holds {len(files)} migrations but names one test for empty-apply and "
            "upgrade. How to fix: name a test that upgrades from the previous migration"
        )
    is_state = directory.resolve() in state_directories
    expected = "ps_state" if is_state else "ps_signing"
    if entry.database != expected:
        relation = "is" if is_state else "is not"
        problems.append(
            f"{key}: is registered as {entry.database} but {relation} a "
            f"ps_service.state_migrations source. How to fix: set database to {expected}"
        )
    for field, node_id in (
        ("empty_apply_test", entry.empty_apply_test),
        ("upgrade_test", entry.upgrade_test),
    ):
        if (why := _resolve(node_id, repo_root)) is not None:
            problems.append(
                f"{key}: {field} {node_id}: {why}. How to fix: {_TEST_FIX.format(field=field)}"
            )
    return problems


def _registry_problems(
    entries: Sequence[MigrationCoverage],
    directories: Sequence[Path],
    repo_root: Path,
    state_directories: frozenset[Path],
) -> list[str]:
    """Return every way `entries` fail to cover the discovered migration `directories`.

    A directory with no entry, an entry for a directory that is gone or registered twice, an
    entry whose recorded newest migration is stale, a multi-file directory naming one test for
    both roles, an entry registered for the wrong database, and a registered test that does not
    exist, is not collected, is not marked `postgres_live` or never runs are each one problem.
    There is no exemption: every directory must be covered.
    """
    counts = Counter(entry.directory for entry in entries)
    problems = [
        f"{key}: has NNNN_*.sql migrations but no entry in guardrails/migration_registry.py. "
        "How to fix: add a MigrationCoverage entry naming its postgres_live empty-apply and "
        "N-1→N upgrade tests"
        for key in (_display(directory, repo_root) for directory in directories)
        if key not in counts
    ]
    problems.extend(
        f"{key}: is registered more than once. How to fix: keep one entry"
        for key, count in counts.items()
        if count > 1
    )
    for entry in {entry.directory: entry for entry in entries}.values():
        problems.extend(_entry_problems(entry, repo_root, state_directories))
    return problems


# --- synthetic trees -------------------------------------------------------------------------

_LIVE_MODULE = """\
import pytest

pytestmark = pytest.mark.postgres_live


def test_empty_apply() -> None: ...


def test_upgrade() -> None: ...
"""

_DIRECTORY = "src/widgets/migrations"
_TEST_FILE = "tests/widgets/test_widgets_live.py"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tree with one single-file migration directory and a marked live test module."""
    _write(tmp_path / _DIRECTORY / "0001_widgets.sql", "CREATE TABLE widgets (id int);\n")
    _write(tmp_path / _TEST_FILE, _LIVE_MODULE)
    return tmp_path


def _widgets_entry(**changes: str) -> MigrationCoverage:
    entry = MigrationCoverage(
        directory=_DIRECTORY,
        database="ps_signing",
        latest_migration="0001_widgets.sql",
        empty_apply_test=f"{_TEST_FILE}::test_empty_apply",
        upgrade_test=f"{_TEST_FILE}::test_upgrade",
    )
    return replace(entry, **changes)


def _check(repo: Path, *entries: MigrationCoverage) -> list[str]:
    return _registry_problems(
        entries, migration_directories(repo / "src"), repo, state_directories=frozenset()
    )


def _resolve_source(repo: Path, source: str, node: str = "test_upgrade") -> str | None:
    _write(repo / _TEST_FILE, source)
    return _resolve(f"{_TEST_FILE}::{node}", repo)


# --- negative controls -----------------------------------------------------------------------


def test_a_fully_covered_synthetic_directory_has_no_problems(repo: Path) -> None:
    assert _check(repo, _widgets_entry()) == []


def test_a_migration_directory_without_an_entry_is_reported(repo: Path) -> None:
    assert _check(repo) == [
        (
            "src/widgets/migrations: has NNNN_*.sql migrations but no entry in "
            "guardrails/migration_registry.py. How to fix: add a MigrationCoverage entry naming "
            "its postgres_live empty-apply and N-1→N upgrade tests"
        )
    ]


def test_a_synthetic_new_directory_fails_the_guard(tmp_path: Path) -> None:
    new_directory = tmp_path / "widgets" / "migrations"
    _write(new_directory / "0001_widgets.sql", "CREATE TABLE widgets (id int);\n")

    problems = _registry_problems(
        REGISTRY,
        [*migration_directories(SERVICE_SOURCE), *migration_directories(tmp_path)],
        REPO_ROOT,
        STATE_DIRECTORIES,
    )

    assert len(problems) == 1
    assert problems[0].startswith(f"{new_directory.resolve()}: has NNNN_*.sql migrations")


def test_a_sql_file_without_a_four_digit_prefix_does_not_make_a_migration_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "foo" / "migrations" / "schema.sql", "")
    _write(tmp_path / "bar" / "migrations" / "001_short.sql", "")
    _write(tmp_path / "baz" / "migrations" / "0001_notes.txt", "")

    assert migration_directories(tmp_path) == []


def test_an_entry_whose_directory_is_gone_is_reported(repo: Path) -> None:
    gone = _widgets_entry(directory="src/gadgets/migrations")

    assert "src/gadgets/migrations: is registered but does not exist. How to fix: remove or " in (
        "\n".join(_check(repo, _widgets_entry(), gone))
    )


def test_an_entry_whose_directory_holds_no_migrations_is_reported(repo: Path) -> None:
    (repo / "src" / "gadgets" / "migrations").mkdir(parents=True)

    problems = _check(repo, _widgets_entry(), _widgets_entry(directory="src/gadgets/migrations"))

    assert problems == [
        (
            "src/gadgets/migrations: is registered but holds no NNNN_*.sql migrations. "
            "How to fix: remove or correct the entry"
        )
    ]


@pytest.mark.parametrize("field", ["empty_apply_test", "upgrade_test"])
def test_an_entry_whose_test_file_does_not_exist_is_reported(repo: Path, field: str) -> None:
    node_id = "tests/widgets/test_missing_live.py::test_upgrade"

    assert _check(repo, _widgets_entry(**{field: node_id})) == [
        (
            f"src/widgets/migrations: {field} {node_id}: test file does not exist. How to fix: "
            f"point {field} at a module-level postgres_live test that runs"
        )
    ]


@pytest.mark.parametrize("field", ["empty_apply_test", "upgrade_test"])
def test_an_entry_whose_test_function_does_not_exist_is_reported(repo: Path, field: str) -> None:
    node_id = f"{_TEST_FILE}::test_nothing_here"

    assert _check(repo, _widgets_entry(**{field: node_id})) == [
        (
            f"src/widgets/migrations: {field} {node_id}: has no module-level test function "
            f"test_nothing_here. How to fix: point {field} at a module-level postgres_live test "
            "that runs"
        )
    ]


@pytest.mark.parametrize("field", ["empty_apply_test", "upgrade_test"])
def test_an_entry_whose_test_lacks_the_postgres_live_marker_is_reported(
    repo: Path, field: str
) -> None:
    _write(repo / "tests/widgets/test_plain.py", "def test_plain() -> None: ...\n")
    node_id = "tests/widgets/test_plain.py::test_plain"

    assert _check(repo, _widgets_entry(**{field: node_id})) == [
        (
            f"src/widgets/migrations: {field} {node_id}: is not marked postgres_live. How to fix: "
            f"point {field} at a module-level postgres_live test that runs"
        )
    ]


def test_a_marker_on_another_function_does_not_count(repo: Path) -> None:
    source = (
        "import pytest\n\n\n"
        "@pytest.mark.postgres_live\ndef test_other() -> None: ...\n\n\n"
        "def test_upgrade() -> None: ...\n"
    )

    assert _resolve_source(repo, source) == "is not marked postgres_live"


@pytest.mark.parametrize(
    "pytestmark",
    [
        pytest.param("pytestmark = pytest.mark.postgres_live", id="single"),
        pytest.param("pytestmark = [pytest.mark.timeout(5), pytest.mark.postgres_live]", id="list"),
        pytest.param(
            "pytestmark: pytest.MarkDecorator = pytest.mark.postgres_live", id="annotated"
        ),
    ],
)
def test_a_module_level_pytestmark_marks_the_test(repo: Path, pytestmark: str) -> None:
    source = f"import pytest\n\n{pytestmark}\n\n\ndef test_upgrade() -> None: ...\n"

    assert _resolve_source(repo, source) is None


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("import pytest\n\n\n@pytest.mark.postgres_live\n", id="attribute"),
        pytest.param("import pytest\n\n\n@pytest.mark.postgres_live()\n", id="call"),
        pytest.param(
            "import pytest\n\n\n@pytest.mark.postgres_live\n"
            '@pytest.mark.parametrize("n", [1, 2])\n',
            id="stacked_with_parametrize",
        ),
        pytest.param("from pytest import mark\n\n\n@mark.postgres_live\n", id="imported_mark"),
    ],
)
def test_a_decorator_marks_the_test(repo: Path, header: str) -> None:
    source = f"{header}def test_upgrade(n: int = 0) -> None: ...\n"

    assert _resolve_source(repo, source) is None


def test_a_parametrized_case_id_resolves_to_its_function(repo: Path) -> None:
    _write(
        repo / _TEST_FILE,
        "import pytest\n\n\n@pytest.mark.postgres_live\n"
        '@pytest.mark.parametrize("n", [1, 2], ids=["one", "two"])\n'
        "def test_upgrade(n: int) -> None: ...\n",
    )

    assert _resolve(f"{_TEST_FILE}::test_upgrade[two]", repo) is None


def test_a_case_id_on_an_unparametrized_function_is_reported(repo: Path) -> None:
    assert (
        _resolve(f"{_TEST_FILE}::test_upgrade[two]", repo)
        == "names case [two] but test_upgrade is not parametrized"
    )


def test_a_class_qualified_test_is_reported(repo: Path) -> None:
    _write(
        repo / _TEST_FILE,
        "import pytest\n\n\nclass TestWidgets:\n"
        "    @pytest.mark.postgres_live\n    def test_upgrade(self) -> None: ...\n",
    )

    assert (
        _resolve(f"{_TEST_FILE}::TestWidgets::test_upgrade", repo)
        == "is not a module-level test function"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "import pytest\n\n\n@pytest.mark.postgres_live\n@pytest.mark.skip\n",
            "is marked skip, so it never proves the migration",
            id="skip_decorator",
        ),
        pytest.param(
            "import pytest\n\n\n@pytest.mark.skipif(True, reason='x')\n"
            "@pytest.mark.postgres_live\n",
            "is marked skipif, so it never proves the migration",
            id="skipif_decorator",
        ),
        pytest.param(
            "import pytest\n\n\n@pytest.mark.xfail(reason='x')\n@pytest.mark.postgres_live\n",
            "is marked xfail, so it never proves the migration",
            id="xfail_decorator",
        ),
        pytest.param(
            "import pytest\n\npytestmark = [pytest.mark.postgres_live, pytest.mark.skip]\n\n\n",
            "is marked skip, so it never proves the migration",
            id="skip_in_pytestmark",
        ),
        pytest.param(
            "import pytest\n\npytest.skip('not here', allow_module_level=True)\n"
            "pytestmark = pytest.mark.postgres_live\n\n\n",
            "is in a module that skips itself",
            id="module_level_skip",
        ),
    ],
)
def test_a_registered_test_that_cannot_run_is_reported(
    repo: Path, source: str, expected: str
) -> None:
    assert _resolve_source(repo, f"{source}def test_upgrade() -> None: ...\n") == expected


def test_a_registered_test_in_a_file_pytest_does_not_collect_is_reported(repo: Path) -> None:
    _write(repo / "tests/widgets/widgets_live.py", _LIVE_MODULE)

    assert (
        _resolve("tests/widgets/widgets_live.py::test_upgrade", repo)
        == "is in a file pytest does not collect (name it test_*.py)"
    )


def test_an_entry_recording_an_older_latest_migration_is_reported(repo: Path) -> None:
    _write(repo / _DIRECTORY / "0002_widget_colour.sql", "ALTER TABLE widgets ADD c text;\n")

    assert _check(repo, _widgets_entry()) == [
        (
            "src/widgets/migrations: newest migration is 0002_widget_colour.sql, the entry records "
            "0001_widgets.sql. How to fix: update latest_migration and register its N-1→N upgrade "
            "test"
        )
    ]


def test_a_multi_file_directory_naming_one_test_for_both_is_reported(repo: Path) -> None:
    _write(repo / _DIRECTORY / "0002_widget_colour.sql", "ALTER TABLE widgets ADD c text;\n")
    entry = _widgets_entry(
        latest_migration="0002_widget_colour.sql", upgrade_test=f"{_TEST_FILE}::test_empty_apply"
    )

    assert _check(repo, entry) == [
        (
            "src/widgets/migrations: holds 2 migrations but names one test for empty-apply and "
            "upgrade. How to fix: name a test that upgrades from the previous migration"
        )
    ]


def test_a_single_file_directory_may_name_one_test_for_both(repo: Path) -> None:
    entry = _widgets_entry(upgrade_test=f"{_TEST_FILE}::test_empty_apply")

    assert _check(repo, entry) == []


def test_a_directory_registered_twice_is_reported(repo: Path) -> None:
    assert _check(repo, _widgets_entry(), _widgets_entry()) == [
        "src/widgets/migrations: is registered more than once. How to fix: keep one entry"
    ]


def test_a_state_source_registered_as_ps_signing_is_reported(repo: Path) -> None:
    problems = _registry_problems(
        [_widgets_entry()],
        migration_directories(repo / "src"),
        repo,
        state_directories=frozenset({(repo / _DIRECTORY).resolve()}),
    )

    assert problems == [
        (
            "src/widgets/migrations: is registered as ps_signing but is a "
            "ps_service.state_migrations source. How to fix: set database to ps_state"
        )
    ]


def test_a_directory_outside_the_state_sources_registered_as_ps_state_is_reported(
    repo: Path,
) -> None:
    assert _check(repo, _widgets_entry(database="ps_state")) == [
        (
            "src/widgets/migrations: is registered as ps_state but is not a "
            "ps_service.state_migrations source. How to fix: set database to ps_signing"
        )
    ]


# --- the real tree ---------------------------------------------------------------------------


def test_every_migration_directory_is_registered_with_marked_tests() -> None:
    problems = _registry_problems(
        REGISTRY, migration_directories(SERVICE_SOURCE), REPO_ROOT, STATE_DIRECTORIES
    )

    assert problems == [], "\n".join(problems)


def test_the_registry_covers_exactly_the_discovered_directories() -> None:
    discovered = {
        _display(directory, REPO_ROOT) for directory in migration_directories(SERVICE_SOURCE)
    }

    assert {entry.directory for entry in REGISTRY} == discovered
    assert len(REGISTRY) == len(discovered)
    assert [field.name for field in fields(MigrationCoverage)] == [
        "directory",
        "database",
        "latest_migration",
        "empty_apply_test",
        "upgrade_test",
    ]
