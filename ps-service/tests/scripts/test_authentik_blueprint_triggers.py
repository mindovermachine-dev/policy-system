"""The trigger list of the blueprint check must name every repository file the check reads.

The check is skipped when no trigger path changed, so a file the check reads but the list omits
could change without the check ever running. This test finds the files statically: path
expressions in `scripts/verify-authentik-blueprint.sh` (`$REPO_ROOT/...`) and path joins rooted at
the repository (`REPO_ROOT / "a" / "b"`, also through `guardrails` imports) in the blueprint
tests, plus the scripts, tests and helper modules themselves.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_TESTS_DIR = REPO_ROOT / "ps-service" / "tests" / "scripts"
GUARDRAILS_DIR = REPO_ROOT / "ps-service" / "tests" / "guardrails"
TRIGGERS = REPO_ROOT / "scripts" / "authentik-blueprint-triggers.txt"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify-authentik-blueprint.sh"

GIT = shutil.which("git") or "git"
SUBPROCESS_TIMEOUT_SECONDS = 60.0
ROOT_MARKER = "<repo>"
GUARDRAILS_PACKAGE = "guardrails"

_SHELL_ROOT_PATH = re.compile(r"\$\{?REPO_ROOT\}?/([\w./-]+)")
_SHELL_RELATIVE_PATH = re.compile(r'^\s*\w*RELPATH="([\w./-]+)"', re.MULTILINE)

type Values = frozenset[str]


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _is_file_path(path: str) -> bool:
    return bool(PurePosixPath(path).suffix)


def shell_reads(text: str) -> set[str]:
    """Return the repository-relative file paths a shell script names, comments excluded."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    named = _SHELL_ROOT_PATH.findall(code) + _SHELL_RELATIVE_PATH.findall(code)
    return {path for path in named if _is_file_path(path)}


def _is_parents_of_file(node: ast.Subscript) -> bool:
    """Match `Path(__file__).resolve().parents[<int>]`."""
    parents = node.value
    if not (isinstance(parents, ast.Attribute) and parents.attr == "parents"):
        return False
    resolved = parents.value
    return (
        isinstance(resolved, ast.Call)
        and isinstance(resolved.func, ast.Attribute)
        and resolved.func.attr == "resolve"
        and ast.unparse(resolved.func.value) == "Path(__file__)"
    )


def _evaluate_parents(node: ast.Subscript, module_file: Path) -> Values | None:
    index = node.slice
    if not (isinstance(index, ast.Constant) and isinstance(index.value, int)):
        return None
    directory = module_file.resolve().parents[index.value]
    if directory == REPO_ROOT:
        return frozenset({ROOT_MARKER})
    if REPO_ROOT in directory.parents:
        return frozenset({f"{ROOT_MARKER}/{_relative(directory)}"})
    return None


def _evaluate_join(node: ast.BinOp, env: dict[str, Values], module_file: Path) -> Values | None:
    left = _evaluate(node.left, env, module_file)
    right = _evaluate(node.right, env, module_file)
    if left is None or right is None or not all(p.startswith(ROOT_MARKER) for p in left):
        return None
    return frozenset(f"{base}/{part}" for base in left for part in right)


def _evaluate(node: ast.expr, env: dict[str, Values], module_file: Path) -> Values | None:
    """Return the repo-rooted paths (or plain strings) an expression can denote, or None."""
    if isinstance(node, ast.Constant):
        return frozenset({node.value}) if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _evaluate_join(node, env, module_file)
    if isinstance(node, ast.Subscript) and _is_parents_of_file(node):
        return _evaluate_parents(node, module_file)
    return None


def _guardrail_file(module: str) -> Path | None:
    package, _, name = module.partition(".")
    return GUARDRAILS_DIR / f"{name}.py" if package == GUARDRAILS_PACKAGE and name else None


def _bindings(module_file: Path) -> dict[str, Values]:
    """Bind module-level names to the paths they denote, following `guardrails` imports."""
    tree = ast.parse(module_file.read_text("utf-8"))
    env: dict[str, Values] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module:
            source = _guardrail_file(statement.module)
            imported = _bindings(source) if source else {}
            env |= {
                a.asname or a.name: imported[a.name] for a in statement.names if a.name in imported
            }
        elif isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
            value = _evaluate(statement.value, env, module_file)
            if value is not None:
                env[statement.targets[0].id] = value
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Tuple | ast.List)
        ):
            items = [e.value for e in node.iter.elts if isinstance(e, ast.Constant)]
            env[node.target.id] = frozenset(str(item) for item in items)
    return env


def python_path_joins(module_file: Path) -> set[str]:
    """Return the repository-relative files a Python module builds paths to."""
    tree = ast.parse(module_file.read_text("utf-8"))
    env = _bindings(module_file)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for path in _evaluate(node, env, module_file) or ():
                relative = path.removeprefix(f"{ROOT_MARKER}/")
                if relative != path and _is_file_path(relative):
                    found.add(relative)
    return found


def imported_guardrail_modules(module_file: Path) -> set[str]:
    """Return the `guardrails` helper modules a module imports, transitively."""
    tree = ast.parse(module_file.read_text("utf-8"))
    modules: set[str] = set()
    for node in tree.body:
        source = (
            _guardrail_file(node.module)
            if isinstance(node, ast.ImportFrom) and node.module
            else None
        )
        if source is not None:
            modules |= {_relative(source)} | imported_guardrail_modules(source)
    return modules


def _blueprint_test_files() -> list[Path]:
    pattern_matches = sorted(SCRIPTS_TESTS_DIR.glob("test_authentik_blueprint*.py"))
    return [*pattern_matches, SCRIPTS_TESTS_DIR / "test_verify_authentik_blueprint.py"]


def blueprint_check_reads() -> set[str]:
    """Return every repository file the blueprint check reads, statically."""
    test_files = _blueprint_test_files()
    reads = shell_reads(VERIFY_SCRIPT.read_text("utf-8"))
    reads |= {_relative(VERIFY_SCRIPT), *(_relative(path) for path in test_files)}
    for path in test_files:
        reads |= python_path_joins(path) | imported_guardrail_modules(path)
    return reads


def trigger_pathspecs() -> list[str]:
    """Return the pathspecs of the trigger list, comments and blank lines removed."""
    lines = (line.partition("#")[0].strip() for line in TRIGGERS.read_text("utf-8").splitlines())
    return [line for line in lines if line]


def files_matching(pathspec: str) -> set[str]:
    """Return the repository files (tracked or not yet ignored-untracked) a pathspec matches."""
    completed = subprocess.run(  # noqa: S603  # fixed git argv; the pathspec is a list entry
        [GIT, "ls-files", "--cached", "--others", "--exclude-standard", "--", pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return set(completed.stdout.splitlines())


def uncovered(reads: set[str], pathspecs: list[str]) -> set[str]:
    """Return the paths no pathspec matches."""
    covered: set[str] = set()
    for pathspec in pathspecs:
        covered |= files_matching(pathspec)
    return reads - covered


def test_every_file_the_blueprint_check_reads_is_covered_by_a_trigger_pathspec() -> None:
    missing = uncovered(blueprint_check_reads(), trigger_pathspecs())

    assert missing == set(), (
        f"add these to scripts/authentik-blueprint-triggers.txt: {sorted(missing)}"
    )


def test_every_trigger_pathspec_matches_a_tracked_file() -> None:
    dead = [spec for spec in trigger_pathspecs() if not files_matching(spec)]

    assert dead == [], f"trigger pathspecs matching no file: {dead}"


def test_a_file_read_but_not_listed_is_reported_as_uncovered() -> None:
    unlisted = "scripts/unlisted-blueprint-helper.sh"

    missing = uncovered(blueprint_check_reads() | {unlisted}, trigger_pathspecs())

    assert unlisted in missing


def test_extractor_finds_dollar_repo_root_and_ast_joins() -> None:
    from_shell = shell_reads(VERIFY_SCRIPT.read_text("utf-8"))
    from_live = python_path_joins(SCRIPTS_TESTS_DIR / "test_authentik_blueprint_apply_live.py")
    verify_tests = SCRIPTS_TESTS_DIR / "test_verify_authentik_blueprint.py"

    assert "charts/policy-system/Chart.yaml" in from_shell
    assert "charts/policy-system/files/authentik-blueprint.yaml" in from_shell
    assert "scripts/verify-authentik-blueprint.sh" in from_live
    assert "charts/policy-system/files/authentik-blueprint.yaml" in from_live
    assert {
        "charts/policy-system/Chart.yaml",
        "charts/policy-system/Chart.lock",
    } <= python_path_joins(verify_tests)
    assert "ps-service/tests/guardrails/chart_render.py" in imported_guardrail_modules(verify_tests)
