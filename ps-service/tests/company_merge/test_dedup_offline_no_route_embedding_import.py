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

Issue #54, S6/B6 widened `resolve_capability_convergence_offline`'s own
`kind` parameter from `Literal["Capability"]` to
`Literal["Capability", "Policy"]` -- `kind` is a runtime parameter of this
ONE function, not a separate function per kind, so the AST scan above
already structurally covers the `kind="Policy"` call path (there is no
second function body a widened `kind` could route through). The runtime
test below is this file's companion proof for that path specifically:
calling with `kind="Policy"` end-to-end, using only an artifact-supplied
embedding and a `single_tenant_graph` fake that carries no
`EmbeddingCaller`-shaped collaborator at all, confirms the offline contract
genuinely holds for Policy too, not merely "the scan found nothing" by
construction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.company_merge.dedup as dedup_module
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.models import BaselineNode
from ps_service.domain_mapper.identity import policy_id

_FORBIDDEN_NAMES = frozenset({"route_embedding", "EmbeddingCaller"})
_TARGET_FUNCTION_NAME = "resolve_capability_convergence_offline"
_THRESHOLD = 0.85


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


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _NoEmbeddingCallerSingleTenantGraph:
    """Satisfies `GraphHandle` structurally -- deliberately carries no
    `EmbeddingCaller`-shaped collaborator anywhere, so this test would fail
    loudly (an `AttributeError`/`TypeError`, not a silent pass) if
    `resolve_capability_convergence_offline`'s `kind="Policy"` path ever
    tried to reach for one.
    """

    def __init__(self, *, policy_rows: list[object]) -> None:
        self._policy_rows = policy_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


def test_resolve_capability_convergence_offline_kind_policy_never_needs_an_embedding_caller() -> (
    None
):
    """Runtime companion (issue #54, S6) to the AST scan above: exercising
    `resolve_capability_convergence_offline(..., kind="Policy", ...)`
    end-to-end -- semantic match via an artifact-supplied incoming embedding
    scored against the existing node's own cached embedding -- succeeds with
    no `EmbeddingCaller`/`call_embedding` collaborator ever passed or
    reachable, proving the offline, never-`route_embedding` contract holds
    for the widened `kind="Policy"` path exactly as it already does for
    Capability.
    """
    existing_title = "Engineering Practices Policy"
    existing_id = policy_id(existing_title)
    incoming_title = "Engineering Practice Policy"
    incoming_id = policy_id(incoming_title)
    assert incoming_id != existing_id

    graph = _NoEmbeddingCallerSingleTenantGraph(
        policy_rows=[[existing_id, existing_title, [1.0, 0.0]]]
    )
    incoming_nodes = (
        BaselineNode(id=incoming_id, properties={"title": incoming_title, "status": "draft"}),
    )

    result = resolve_capability_convergence_offline(
        incoming_nodes,
        incoming_embeddings={incoming_id: (1.0, 0.0)},
        single_tenant_graph=graph,
        threshold=_THRESHOLD,
        kind="Policy",
    )

    assert result.resolutions[0].match_kind == "semantic"
    assert result.resolutions[0].canonical_id == existing_id
