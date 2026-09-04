"""AST-scan proof (PLAN.md Slice 5.4, D6) that
`ps_service.company_merge.dedup.resolve_capability_convergence_offline`
never references `route_embedding`/`EmbeddingCaller` anywhere in its own
body -- mirrors `tests/company_merge/test_identity_reuse.py`'s AST-scan
convention (`ast.parse`/`ast.walk`, not a substring grep), scoped to this
one function's subtree rather than the whole module, since `dedup.py`'s
module scope legitimately imports `route_embedding`/`EmbeddingCaller` for
`find_best_semantic_match`/`dedupe_canonical_nodes` (the LIVE dedup path) --
only `resolve_capability_convergence_offline` itself (the OFFLINE path, D6)
must never touch either name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.company_merge.dedup as dedup_module

_FORBIDDEN_NAMES = frozenset({"route_embedding", "EmbeddingCaller"})
_TARGET_FUNCTION_NAME = "resolve_capability_convergence_offline"


def _find_function_def(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r} found")


def _referenced_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_resolve_capability_convergence_offline_never_references_forbidden_names() -> None:
    module_path = Path(dedup_module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    function_def = _find_function_def(tree, _TARGET_FUNCTION_NAME)
    referenced = _referenced_names(function_def)

    forbidden_found = _FORBIDDEN_NAMES & referenced
    assert not forbidden_found, (
        f"{_TARGET_FUNCTION_NAME} references forbidden name(s): {sorted(forbidden_found)!r}"
    )


def test_function_def_lookup_finds_the_real_target_function() -> None:
    """Regression guard: `_find_function_def` actually locates a real
    top-level function in `dedup.py`, not silently matching nothing.
    """
    module_path = Path(dedup_module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    function_def = _find_function_def(tree, _TARGET_FUNCTION_NAME)

    assert function_def.name == _TARGET_FUNCTION_NAME


def test_referenced_names_flags_a_hypothetical_forbidden_reference() -> None:
    """Positive case: a hypothetical call to `route_embedding` inside a
    function body would be flagged -- proving the scan actually catches a
    reference, not just an absence of any name at all.
    """
    tree = ast.parse("def f():\n    return route_embedding(x)\n")
    function_def = _find_function_def(tree, "f")

    assert "route_embedding" in _referenced_names(function_def)
