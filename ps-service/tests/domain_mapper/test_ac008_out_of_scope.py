"""Proof that `ps_service.domain_mapper` never names a Policy/Standard/Control governance
artifact anywhere in the package.

Governance artifacts are authored directly in the internal-seed intake document and minted and
persisted by `ps_service.ingestion.adapters.internal_seed`, a different package. No file under
`ps_service.domain_mapper/` legitimately names any of these literals, so the whole-package ban is
correct: it fails the moment `derive_governance_artifacts` (or any of the three
governance-derivation system prompts) reappears anywhere in this package's production source.

An AST-based scan (not a substring grep) of every `.py` file actually delivered under
`ps_service/domain_mapper/` (source only, found by walking the real filesystem --
`_files_to_scan`), asserting no string-literal `Constant` node whose value is one of
`"Policy"`/`"Standard"`/`"Control"`/`"GOVERNED_BY"`/`"SUPPORTED_BY"`/`"IMPLEMENTED_BY"` appears
ANYWHERE in any of those files (module/class/function docstrings excluded).

The scan walks the ENTIRE module tree for a matching `ast.Constant`, not just a conditional
construct's *decision* subtree (`ast.If`/`IfExp`/`While`/`Assert`/`Compare`/`Match`): the concern
here is broader than regulation-name-conditional branching logic -- no such string literal
ANYWHERE in the package. The docstring-exclusion mechanism itself (`_docstring_constant_ids`)
follows the ingestion package's scan. Comments are already invisible to `ast` (discarded at
tokenization), so no separate mechanism is needed for those.
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
    rather than a hardcoded list. Used by both the literal-ban test and
    `test_files_to_scan_covers_every_known_domain_mapper_module`.
    """
    domain_mapper_root = Path(domain_mapper_package.__file__).parent
    return sorted(domain_mapper_root.rglob("*.py"))


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """`id()`s of every module/class/function docstring's `ast.Constant`
    node -- the first statement of a `Module`/`ClassDef`/`FunctionDef`/
    `AsyncFunctionDef` body, when it is a bare string-literal expression.

    Same mechanism and exclusion rule as
    `ps-service/tests/ingestion/test_pipeline.py::_docstring_constant_ids`.
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
    docstring for why that restriction does not apply to this check).
    Docstring `Expr` nodes are excluded via `_docstring_constant_ids`.
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


def test_domain_mapper_package_never_names_governance_artifacts() -> None:
    """Literal-search proof: no file anywhere under `ps_service.domain_mapper`
    may name a Policy/Standard/Control governance artifact (whole-package ban).
    """
    for path in _files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = _find_forbidden_literals(tree)
        assert not violations, (
            f"{path}: forbidden AC-BI-009/AC-BI-012 literal found at "
            f"{[(v.value, v.lineno) for v in violations]}"
        )


def test_files_to_scan_covers_every_known_domain_mapper_module() -> None:
    """Guards against `_files_to_scan` silently narrowing down to a
    hardcoded-looking subset: the filesystem walk must find every module
    this package ships.
    """
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
    decision subtree (see this module's docstring).
    """
    tree = ast.parse('_LABEL = "Policy"\n')

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Policy"]


def test_find_forbidden_literals_flags_a_hypothetical_conditional_too() -> None:
    """A forbidden literal used inside a conditional's decision is also
    flagged -- the whole-tree scan is a superset of a conditional-only
    check, not a narrower one.
    """
    tree = ast.parse(
        "def f(label):\n    if label == 'Control':\n        return True\n    return False\n"
    )

    violations = _find_forbidden_literals(tree)

    assert [v.value for v in violations] == ["Control"]


def test_find_forbidden_literals_ignores_docstring_examples() -> None:
    """Negative case: a docstring mentioning these terms as illustrative
    prose (exactly like this test module's own docstring) is not flagged.
    """
    tree = ast.parse(
        '"""This component never derives Policy, Standard, or Control '
        "nodes, and never writes GOVERNED_BY, SUPPORTED_BY, or "
        'IMPLEMENTED_BY edges."""\n'
        "def f() -> None:\n"
        "    pass\n"
    )

    violations = _find_forbidden_literals(tree)

    assert violations == []
