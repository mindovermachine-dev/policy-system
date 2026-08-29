"""Identity-reuse proof (PLAN_REVIEWED.md §3 / §10 Increment 3):
`ps_service.company_merge` never reimplements Domain Mapper's `capability_id`
hash function -- it imports it directly from
`ps_service.domain_mapper.identity`, so its exact-key match computes *the
same* id Domain Mapper already used to write the baseline graph's Capability
node ids. (Since issue #42 Company Merge dedupes Capability only; Obligation
is Role-scoped and passed through, so `obligation_id` is no longer imported
here -- but a fresh *definition* of it in this package is still forbidden.)

Two independent proofs:

1. An AST-based scan (not a substring grep) of every `.py` file actually
   delivered under `ps_service/company_merge/` (`pathlib.Path(...).rglob(
   "*.py")`, not a hardcoded file list -- mirrors #15's own
   `test_ac008_out_of_scope.py` scan precedent,
   `ps-service/tests/domain_mapper/test_ac008_out_of_scope.py`), asserting
   no function named `obligation_id`/`capability_id`/`_hash`/`_slug` is
   ever *defined* (`ast.FunctionDef`) anywhere in this package.
2. A direct, byte-for-byte comparison: calling
   `ps_service.domain_mapper.identity.capability_id` with a fixed input
   produces exactly the value this package would use for its own exact-key
   match.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.company_merge as company_merge_package
from ps_service.company_merge.dedup import capability_id
from ps_service.domain_mapper.identity import (
    capability_id as domain_mapper_capability_id,
)

_FORBIDDEN_FUNCTION_NAMES = frozenset({"obligation_id", "capability_id", "_hash", "_slug"})


def _files_to_scan() -> list[Path]:
    """Every `.py` file actually delivered under `ps_service/company_merge/`
    (source only -- `ps-service/src/ps_service/company_merge/`, never
    `tests/`), found by walking the real filesystem (`rglob("*.py")`)
    rather than a hardcoded list -- mirrors #15's `_files_to_scan`
    precedent exactly."""
    company_merge_root = Path(company_merge_package.__file__).parent
    return sorted(company_merge_root.rglob("*.py"))


def _find_forbidden_function_defs(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Flags every `def`/`async def` whose name is one of
    `_FORBIDDEN_FUNCTION_NAMES`, anywhere in the module -- a definition, not
    a call or import (importing `obligation_id`/`capability_id` from
    `ps_service.domain_mapper.identity` is exactly what this package is
    required to do; only a fresh *definition* of one of these names would
    indicate reimplementation)."""
    violations: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _FORBIDDEN_FUNCTION_NAMES:
            violations.append(node)
    return violations


def test_no_identity_function_defined_in_company_merge_package() -> None:
    for path in _files_to_scan():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations = _find_forbidden_function_defs(tree)
        assert not violations, (
            f"{path}: forbidden identity function definition found at "
            f"{[(v.name, v.lineno) for v in violations]}"
        )


def test_files_to_scan_covers_every_known_company_merge_module() -> None:
    """Guards against `_files_to_scan` silently narrowing down to a
    hardcoded-looking subset -- mirrors #15's own
    `test_files_to_scan_covers_every_known_domain_mapper_module` regression
    guard."""
    scanned_relative_paths = {
        str(path.relative_to(Path(company_merge_package.__file__).parent)) for path in _files_to_scan()
    }
    expected = {"__init__.py", "errors.py", "models.py", "similarity.py", "dedup.py"}
    assert expected <= scanned_relative_paths


def test_find_forbidden_function_defs_flags_a_hypothetical_reimplementation() -> None:
    """Positive case: a hypothetical fresh `def obligation_id(...)` inside
    this package would be flagged -- proving the scan actually catches a
    reimplementation, not just an absence of any function at all."""
    tree = ast.parse("def obligation_id(text):\n    return text\n")

    violations = _find_forbidden_function_defs(tree)

    assert [v.name for v in violations] == ["obligation_id"]


def test_find_forbidden_function_defs_ignores_unrelated_function_names() -> None:
    """Negative case: an ordinary, unrelated function definition is not
    flagged."""
    tree = ast.parse("def read_existing_canonical_index(label):\n    return ()\n")

    violations = _find_forbidden_function_defs(tree)

    assert violations == []


# --- Direct byte-for-byte comparison against domain_mapper's own functions ---


def test_capability_id_matches_domain_mapper_identity_exactly() -> None:
    name = "Security Logging"
    assert capability_id(name) == domain_mapper_capability_id(name)


def test_company_merge_dedup_reexports_the_same_function_object() -> None:
    """Byte-for-byte identity, not just behavioral equality: `dedup.py`
    imports (not wraps/reimplements) Domain Mapper's own function object."""
    assert capability_id is domain_mapper_capability_id
