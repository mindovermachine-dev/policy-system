"""AC-015 scope guards for `mcp_server.py` (PLAN_REVIEWED.md §6, Batch 6; §9).

These prove -- structurally, by parsing the module's AST -- that the MCP
surface stays stdio-only with no auth, no network transport, and no query
timeout / result-size cap. They inspect `ast.Call` / `ast.keyword` /
`ast.Constant` nodes and MUST NOT substring-scan the source: a bare scan
false-fails on the `cypher` docstring's `CREATE/MERGE/...` clause list and on
the module docstring's "no query timeout / result-size cap" phrasing (F-03,
Residual risk 6).
"""

from __future__ import annotations

import ast
import inspect

from ps_service.mcp_interface import mcp_server


def _module_ast() -> ast.Module:
    return ast.parse(inspect.getsource(mcp_server))


def _func_renders_as(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _calls(tree: ast.Module) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _keyword_names(tree: ast.Module) -> list[str]:
    return [
        node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg is not None
    ]


def _call_arg_string_constants(tree: ast.Module) -> list[str]:
    """Every string literal passed positionally or as a keyword value to any
    call -- deliberately NOT docstrings or module-level assignments.
    """
    values: list[str] = []
    for call in _calls(tree):
        operands = list(call.args) + [kw.value for kw in call.keywords]
        for operand in operands:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                values.append(operand.value)
    return values


def test_mcpserver_ctor_has_no_auth_kwargs() -> None:
    tree = _module_ast()
    ctor_calls = [c for c in _calls(tree) if _func_renders_as(c.func) == "MCPServer"]
    assert len(ctor_calls) == 1

    kwarg_names = {kw.arg for kw in ctor_calls[0].keywords}
    for forbidden in ("auth", "auth_server_provider", "token_verifier"):
        assert forbidden not in kwarg_names


def test_no_network_transport_kwarg() -> None:
    tree = _module_ast()

    assert "transport" not in _keyword_names(tree)

    for value in _call_arg_string_constants(tree):
        assert value not in {"sse", "streamable-http"}


def test_no_timeout_or_row_limit_symbols() -> None:
    tree = _module_ast()

    keyword_names = _keyword_names(tree)
    for forbidden in ("timeout", "max_rows", "row_limit"):
        assert forbidden not in keyword_names

    assert not hasattr(mcp_server, "max_rows")
    assert not hasattr(mcp_server, "row_limit")

    for value in _call_arg_string_constants(tree):
        assert " LIMIT " not in value
