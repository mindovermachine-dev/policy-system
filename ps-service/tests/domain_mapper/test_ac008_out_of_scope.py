"""AC-008 proof: `DeriveGovernanceArtifacts` (Policy/Standard/Control
derivation, internal-source only) is out of scope for this issue and is not
implemented anywhere in `ps_service.domain_mapper` (PLAN_REVIEWED.md §10 /
§11 Increment 18).

An AST-based scan (not a substring grep) of every `.py` file actually
delivered under `ps_service/domain_mapper/` (`pathlib.Path(...).rglob(
"*.py")`, not a hardcoded file list -- mirrors issue #14's own AC-006 scan
precedent, `ps-service/tests/ingestion/test_pipeline.py`), asserting no
string-literal `Constant` node whose value is one of `"Policy"`/
`"Standard"`/`"Control"`/`"GOVERNED_BY"`/`"SUPPORTED_BY"`/`"IMPLEMENTED_BY"`
appears ANYWHERE in the package's source (module/class/function/docstrings
excluded).

**Deliberate divergence from #14's own scan mechanism, and why:** #14's
`test_no_regulation_name_conditionals_in_ingestion_package` only ever walks
a conditional/branching construct's *decision* subtree (`ast.If`/`IfExp`/
`While`/`Assert`/`Compare`/`Match`) -- appropriate for THAT check, whose
concern was regulation-name-conditional branching logic specifically. This
issue's Increment 18 has a broader concern (CONTEXT.md AC-008 / PLAN_REVIEWED.md
§10): no `Policy`/`Standard`/`Control`/`GOVERNED_BY`/`SUPPORTED_BY`/
`IMPLEMENTED_BY` string literal ANYWHERE in the delivered package, not only
inside a conditional's decision -- so this scan walks the ENTIRE module tree
for a matching `ast.Constant`, not just conditional subtrees. The docstring-
exclusion mechanism itself (`_docstring_constant_ids`) is copied verbatim
from #14's precedent, per this batch's explicit instruction to reuse the
same AST-based exclusion mechanism, not invent a different one. Comments
are already invisible to `ast` (discarded at tokenization) -- no separate
mechanism is needed for those, exactly as in #14's own scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.domain_mapper as domain_mapper_package

_FORBIDDEN_LITERALS = frozenset(
    {"Policy", "Standard", "Control", "GOVERNED_BY", "SUPPORTED_BY", "IMPLEMENTED_BY"}
)

_DOCSTRING_HOST_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _files_to_scan() -> list[Path]:
    """Every `.py` file actually delivered under `ps_service/domain_mapper/`
    (source only -- `ps-service/src/ps_service/domain_mapper/`, never
    `tests/`), found by walking the real filesystem (`rglob("*.py")`)
    rather than a hardcoded list -- mirrors #14's `_files_to_scan`
    precedent exactly, so this must catch a violation in ANY module under
    the package, including `adapters/**`, not just a specific file."""
    domain_mapper_root = Path(domain_mapper_package.__file__).parent
    return sorted(domain_mapper_root.rglob("*.py"))


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """`id()`s of every module/class/function docstring's `ast.Constant`
    node -- the first statement of a `Module`/`ClassDef`/`FunctionDef`/
    `AsyncFunctionDef` body, when it is a bare string-literal expression.

    Copied verbatim (same mechanism, same exclusion rule) from #14's own
    `ps-service/tests/ingestion/test_pipeline.py::_docstring_constant_ids`
    -- this batch's instructions require staying consistent with that
    precedent rather than inventing a different exclusion rule.
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


def _find_forbidden_literals(tree: ast.AST) -> list[ast.Constant]:
    """AST-based whole-tree walk, not a substring `"Policy" not in source`
    check. Flags every string-literal `Constant` node whose value is one of
    `_FORBIDDEN_LITERALS`, anywhere in the module -- not restricted to a
    conditional construct's decision subtree (see this module's own
    docstring for why that restriction, present in #14's own scan, does not
    apply to this broader AC-008 check). Docstring `Expr` nodes are
    excluded via `_docstring_constant_ids`, same mechanism as #14's scan.
    """
    docstring_ids = _docstring_constant_ids(tree)
    violations: list[ast.Constant] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _FORBIDDEN_LITERALS
            and id(node) not in docstring_ids
        ):
            violations.append(node)
    return violations


def test_no_governance_artifact_literals_in_domain_mapper_package() -> None:
    for path in _files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = _find_forbidden_literals(tree)
        assert not violations, (
            f"{path}: forbidden AC-008 literal found at "
            f"{[(v.value, v.lineno) for v in violations]}"
        )


def test_files_to_scan_covers_every_known_domain_mapper_module() -> None:
    """Guards against `_files_to_scan` silently narrowing down to a
    hardcoded-looking subset -- mirrors #14's own
    `test_files_to_scan_covers_every_known_ingestion_module` regression
    guard, asserting the rglob walk actually finds every module Batches
    1-9 delivered."""
    scanned_relative_paths = {
        str(path.relative_to(Path(domain_mapper_package.__file__).parent))
        for path in _files_to_scan()
    }
    expected = {
        "__init__.py",
        "errors.py",
        "models.py",
        "identity.py",
        "prompts.py",
        "extraction.py",
        "derivation.py",
        "falkordb_client.py",
        "graph_writer.py",
        "adapters/__init__.py",
        "adapters/base.py",
        "adapters/cellar_eli.py",
    }
    assert expected <= scanned_relative_paths


def test_find_forbidden_literals_flags_a_hypothetical_literal_anywhere_in_the_module() -> None:
    """Positive case: an ordinary (non-conditional, non-docstring) string
    literal assignment naming a forbidden governance-artifact term is
    flagged -- proving the scan is NOT restricted to a conditional's
    decision subtree (the deliberate divergence from #14's own scan, see
    this module's docstring)."""
    tree = ast.parse('_LABEL = "Policy"\n')

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Policy"]


def test_find_forbidden_literals_flags_a_hypothetical_conditional_too() -> None:
    """A forbidden literal used inside a conditional's decision is also
    flagged -- the broader whole-tree scan is a superset of #14's own
    conditional-only check, not a replacement that narrows coverage."""
    tree = ast.parse(
        "def f(label):\n"
        "    if label == 'Control':\n"
        "        return True\n"
        "    return False\n"
    )

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Control"]


def test_find_forbidden_literals_ignores_docstring_examples() -> None:
    """Negative case: a docstring mentioning these terms as illustrative
    prose (e.g. explaining what AC-008 excludes, exactly like this test
    module's own docstring) is not flagged."""
    tree = ast.parse(
        '"""This component never derives Policy, Standard, or Control '
        'nodes, and never writes GOVERNED_BY, SUPPORTED_BY, or '
        'IMPLEMENTED_BY edges."""\n'
        "def f() -> None:\n"
        "    pass\n"
    )

    violations = _find_forbidden_literals(tree)

    assert violations == []
