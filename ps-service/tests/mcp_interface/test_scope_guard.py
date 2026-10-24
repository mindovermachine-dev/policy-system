"""Scope guards for the MCP tool surface (`tool_core.py` holds the `MCPServer(...)` call).

These prove -- structurally, by parsing the modules' AST -- that the tool surface stays a pure
surface definition: it binds no transport and constructs its module-level `MCPServer(...)`
singleton, in `tool_core.py`, with zero auth kwargs, forever. The `mcp_server.py` front door
re-exports that `server`. Transport (and real per-user auth) both
belong to the sibling `http_transport.py` module, so neither may leak back
into these modules. They inspect `ast.Call` / `ast.keyword` / `ast.Constant`
nodes and MUST NOT substring-scan the source: a bare scan false-fails on
the `cypher` docstring's `CREATE/MERGE/...` clause list (F-03, Residual
risk 6).

These guards describe the tool-surface modules' own source only (`tool_core.py`, the
`*_tools.py` tool-group modules and `mcp_server.py`, found by glob). The Streamable HTTP
transport -- the
only transport, since MCP's stdio entrypoint was removed -- and the
real auth wiring both live in the sibling
`ps_service.mcp_interface.http_transport` module, out of this file's AST
entirely, so neither triggers nor is covered by these assertions.

`test_mcpserver_ctor_has_no_auth_kwargs` stays exactly as written (not
deleted, not weakened) even though real auth is wired: the design keeps the module-level
`MCPServer(...)` call itself permanently free of
`auth`/`auth_server_provider`/`token_verifier` kwargs -- auth is
armed on the already-constructed `server` object, after the fact, inside
`http_transport.build_streamable_http_app` (see that module and
`test_mcp_wires_token_verifier_after_construction_when_auth_configured`
below), specifically so no I/O or fail-closed check ever runs at bare
module-import time. This AST guard is what keeps that invariant true by
construction; the behavioral test below is what proves the *runtime*
post-construction wiring actually happens.
"""

from __future__ import annotations

import ast

from mcp_interface._source_modules import merged_tool_tree, tool_source_paths
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.mcp_interface import http_transport, mcp_server


def _module_ast() -> ast.Module:
    """The union of the tool-surface modules (not the transport)."""
    return merged_tool_tree()


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


_FAKE_AUTH_CONTEXT = AuthContext(
    issuer="http://127.0.0.1:1/issuer-never-fetched",
    audience="ps-service",
    cli_client_id=None,
    scopes=(),
    jwks_uri="http://127.0.0.1:1/jwks.json",
    allowed_algorithms=frozenset({"RS256"}),
)


def test_mcp_wires_token_verifier_after_construction_when_auth_configured() -> None:
    """Behavioral complement to the AST-based guard that stays unchanged (see
    module docstring). Proves `build_streamable_http_app` actually arms the
    module-level `server` singleton's auth -- a direct runtime check, not a
    source-shape scan, since what matters now is that the mutation really
    happens, not merely that the ctor call stays clean.

    Constructing `PsTokenVerifier` here never performs network I/O (`jwt.
    PyJWKClient`'s constructor is lazy -- see `verifier.py`), so this test
    needs no mock OIDC provider; `verify_token` itself is never called.
    """
    verifier = PsTokenVerifier(_FAKE_AUTH_CONTEXT)

    http_transport.build_streamable_http_app(
        server=mcp_server.server,
        host="127.0.0.1",
        verifier=verifier,
        auth_context=_FAKE_AUTH_CONTEXT,
    )

    assert mcp_server.server._token_verifier is verifier  # pyright: ignore[reportPrivateUsage]
    assert mcp_server.server.settings.auth is not None
    assert str(mcp_server.server.settings.auth.issuer_url).rstrip("/") == (
        _FAKE_AUTH_CONTEXT.issuer.rstrip("/")
    )
    assert mcp_server.server.settings.auth.resource_server_url is None
    assert mcp_server.server.settings.auth.required_scopes is None


def test_mcp_disarms_token_verifier_when_auth_not_configured() -> None:
    """The bypass-path companion: `verifier=None`/`auth_context=None` (the
    local-test bypass active) leaves -- or resets -- `server`'s auth to
    fully disarmed. Explicitly re-arms first, then disarms, to prove the
    disarm is an unconditional reset, not merely "never got the chance to
    arm" -- see `http_transport.build_streamable_http_app`'s own docstring
    on why disarming must be unconditional (the shared, process-lifetime
    `server` singleton would otherwise leak a stale verifier across
    unrelated calls in the same process/test run).
    """
    verifier = PsTokenVerifier(_FAKE_AUTH_CONTEXT)
    http_transport.build_streamable_http_app(
        server=mcp_server.server,
        host="127.0.0.1",
        verifier=verifier,
        auth_context=_FAKE_AUTH_CONTEXT,
    )
    assert mcp_server.server._token_verifier is verifier  # pyright: ignore[reportPrivateUsage]

    http_transport.build_streamable_http_app(
        server=mcp_server.server, host="127.0.0.1", verifier=None, auth_context=None
    )

    assert mcp_server.server._token_verifier is None  # pyright: ignore[reportPrivateUsage]
    assert mcp_server.server.settings.auth is None


def test_the_module_holding_the_mcpserver_call_is_named_in_this_docstring() -> None:
    holders = [
        path.name
        for path in tool_source_paths()
        if any(
            isinstance(node, ast.Call) and _func_renders_as(node.func) == "MCPServer"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]

    assert len(holders) == 1
    assert __doc__ is not None
    assert holders[0] in __doc__
