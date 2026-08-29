"""Shared AST-scan helpers for the regulation-independence check (AC-011).

An AST-based scan (not a substring grep) of every `.py` file actually
delivered under a package, asserting that no string literal naming a
regulation (`CRA`/`GDPR`/`NIS2`) or a real CELEX identifier of the
instruments ingested today (`32024R2847`/`32016R0679`/`32022L2555`) appears
inside a conditional/branching construct's *decision* anywhere in that
package. Docstring prose never trips the check: the scan only ever walks the
decision subtree of a conditional construct (an `if`/`elif` test, a
ternary's test, a `while`/`assert` condition, a bare `Compare` node, a
`match` statement's case patterns) and a docstring statement can never
structurally appear inside one; a `_docstring_constant_ids` exclusion is
layered on top anyway, per PLAN_REVIEWED.md's requirement — defensive, not
load-bearing given the above.

**Why this module exists (PLAN_REVIEWED.md §1.6, flaw 13).** This is only
the 2nd occurrence of the scan machinery — one short of L2's
extract-on-3rd-repeat threshold. It is extracted early, deliberately,
because (a) it is ~90 lines of non-obvious AST logic, not a 3-line idiom —
a silently drifted copy is exactly the AC-011 failure mode; (b) AC-011's
wording ("the scan is *extended to cover* `change_monitor`") asks for one
scan run over two packages, not two independent scans; (c) it is the right
abstraction: a single stable predicate with zero expected divergence
between call sites. Both `tests/ingestion/test_pipeline.py` and
`tests/change_monitor/test_regulation_independence.py` import from here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

FORBIDDEN_REGULATORY_INSTRUMENT_NAMES = frozenset({"CRA", "GDPR", "NIS2"})
FORBIDDEN_CELEX_IDENTIFIERS = frozenset({"32024R2847", "32016R0679", "32022L2555"})
FORBIDDEN_LITERALS = FORBIDDEN_REGULATORY_INSTRUMENT_NAMES | FORBIDDEN_CELEX_IDENTIFIERS

_DOCSTRING_HOST_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """`id()`s of every module/class/function docstring's `ast.Constant`
    node — the first statement of a `Module`/`ClassDef`/`FunctionDef`/
    `AsyncFunctionDef` body, when it is a bare string-literal expression.

    Kept as an explicit, separate exclusion (per PLAN_REVIEWED.md's B3 fix
    wording) even though `find_forbidden_literals` below only ever walks a
    conditional construct's *decision* subtree — a docstring statement can
    never structurally be part of one, so this exclusion is
    defense-in-depth, not load-bearing; see this module's own docstring.
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_HOST_TYPES):
            first_statement = node.body[0] if node.body else None
            if (
                isinstance(first_statement, ast.Expr)
                and isinstance(first_statement.value, ast.Constant)
                and isinstance(first_statement.value.value, str)
            ):
                docstring_ids.add(id(first_statement.value))
    return docstring_ids


def _conditional_decision_subtrees(tree: ast.AST) -> list[ast.AST]:
    """Every expression subtree that is a conditional/branching construct's
    *decision* — never its body/orelse — per PLAN_REVIEWED.md's "ast.If /
    ast.Compare / any other executable context" wording: an `if`/`elif`
    test, a ternary's test, a `while`/`assert` condition, a bare `Compare`
    node (covers `==`/`!=`/`in`/`not in`/`is`/... wherever it occurs, even
    outside an explicit `if`), and a `match` statement's case patterns.
    """
    subtrees: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            subtrees.append(node.test)
        elif isinstance(node, ast.Compare):
            subtrees.append(node)
        elif isinstance(node, ast.Match):
            subtrees.extend(case.pattern for case in node.cases)
    return subtrees


def find_forbidden_literals(tree: ast.AST) -> list[ast.Constant]:
    """AST-based walk, not a substring `"CRA" not in source` check.

    Flags a string-literal `Constant` node whose value is a forbidden
    literal — a regulation name (`CRA`/`GDPR`/`NIS2`) or one of the real
    CELEX identifiers of the instruments ingested today
    (`32024R2847`/`32016R0679`/`32022L2555`) — when it appears inside a
    conditional/branching construct's decision
    (`_conditional_decision_subtrees`) — i.e. a genuine
    `short_name == "CRA"`-shaped regulation-name conditional. Comments are
    already invisible to `ast` (discarded at tokenization); docstring `Expr`
    nodes are additionally, explicitly excluded (`_docstring_constant_ids`).
    """
    docstring_ids = _docstring_constant_ids(tree)
    seen: set[int] = set()
    violations: list[ast.Constant] = []
    for subtree in _conditional_decision_subtrees(tree):
        for node in ast.walk(subtree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in FORBIDDEN_LITERALS
                and id(node) not in docstring_ids
                and id(node) not in seen
            ):
                seen.add(id(node))
                violations.append(node)
    return violations


def scan_package_for_forbidden_conditionals(
    package: ModuleType,
) -> list[tuple[Path, ast.Constant]]:
    """Scan every `.py` file actually delivered under `package` for a
    forbidden-literal conditional.

    Walks the real filesystem (`Path(package.__file__).parent.rglob("*.py")`)
    rather than a hardcoded module list, so a violation introduced in any
    module — including one added after this scan was written — is caught.
    Returns a flat list of `(path, violating ast.Constant node)` pairs
    across all files; an empty list means the whole package is clean.
    """
    package_file = package.__file__
    assert package_file is not None, f"package {package.__name__!r} has no __file__"
    package_root = Path(package_file).parent
    violations: list[tuple[Path, ast.Constant]] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend((path, node) for node in find_forbidden_literals(tree))
    return violations
