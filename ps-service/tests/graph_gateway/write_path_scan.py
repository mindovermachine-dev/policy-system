"""The AST scan behind the "no write path to FalkorDB" guards.

Every component whose graph writes were routed through the Graph Write Gateway has a guard test
that scans its package with `scan_source`: no module may open a FalkorDB connection of its own,
name the gateway's apply machinery, or pass write Cypher to a graph handle. The scan is an AST
walk. It looks at the text handed to a call named `query`, `ro_query`, `_execute_query`,
`_read_query`, `read_rows` or `query_rows`: a string literal, the constant parts of an f-string, or
a name that resolves to such text. It never looks at docstrings or prose, so a module may
*describe* `MERGE` without being flagged, and it matches the uppercase keywords only, so the
lowercase `'merged'` of a read stays clean.

There is one policy and no way to relax it: no allowance for a connection helper, a type-only
import or a read-side constant. Only the `graph_gateway` package may touch the driver or the
write-capable factory modules, and it is not scanned (`test_gateway_only_graph_access.py`).
Write Cypher, the applier and cypher imports and `apply_entries` are always forbidden.

`reaches` is the positive half: whether a function calls a target directly or through same-module
functions (by name, no mock), so a guard can prove every writer ends at the gateway sink.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["called_name", "calls_by_function", "iter_service_modules", "reaches", "scan_source"]

QUERY_CALLS = frozenset(
    {"query", "ro_query", "_execute_query", "_read_query", "read_rows", "query_rows"}
)
# `select_graph`, `connect` and `connect_from_config` are also the names of read-only delegates, so
# they are not connection calls by name: the write-capable ones are reachable only through the
# factory modules below, whose import is forbidden.
CONNECTION_CALLS = frozenset({"FalkorDB"})
WRITE_CAPABLE_GATEWAY_MODULES = (
    "ps_service.graph_gateway.connection",
    "ps_service.graph_gateway.key_port",
)
WRITE_CYPHER = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DROP|DETACH|FOREACH)\b")
_ALWAYS_FORBIDDEN_IMPORT_PREFIXES = (
    "ps_service.graph_gateway.applier",
    "ps_service.graph_gateway.cypher",
    *WRITE_CAPABLE_GATEWAY_MODULES,
)
_CONNECTION_IMPORT_PREFIXES = ("falkordb", "redis")
_ALLOWED_REDIS_PREFIX = "redis.exceptions"
_FORBIDDEN_NAMES = frozenset({"apply_entries"})


def iter_service_modules(root: Path) -> list[Path]:
    """Every Python module under `root`, sorted: the one walk shared by the architecture guard
    (`test_gateway_only_graph_access.py`) and the Cypher vocabulary scan, so they cannot
    diverge on which files make up the service.
    """
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def called_name(call: ast.Call) -> str | None:
    """The simple name a call goes to (`f(x)` and `o.f(x)` are both `f`), else `None`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _literal_text(node: ast.expr, names: dict[str, list[str]]) -> list[str]:
    """The constant text `node` stands for (placeholders dropped); `[]` when it is not text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            " ".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        ]
    if isinstance(node, ast.Name):
        return names.get(node.id, [])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":  # TEMPLATE.format(kind=kind): the template is the text
            return _literal_text(node.func.value, names)
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [
            f"{left} {right}"
            for left in _literal_text(node.left, names)
            for right in _literal_text(node.right, names)
        ]
    return []


def _text_assignments(tree: ast.Module) -> dict[str, list[str]]:
    """Every name assigned text anywhere in the module (module constants and locals), resolved."""
    names: dict[str, list[str]] = {}
    assignments = [
        (target.id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assignments += [
        (node.target.id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    ]
    for _ in range(3):  # a name built from another name resolves within a few passes
        for name, value in assignments:
            texts = _literal_text(value, names)
            if texts:
                names[name] = list({*names.get(name, []), *texts})
    return names


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.module is None or node.level != 0:
        return []
    return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]


def _forbidden_import(module: str) -> bool:
    if module.startswith(_ALWAYS_FORBIDDEN_IMPORT_PREFIXES):
        return True
    if module.startswith("redis"):
        return not module.startswith(_ALLOWED_REDIS_PREFIX)
    return module.startswith(_CONNECTION_IMPORT_PREFIXES)


def _call_findings(node: ast.Call, names: dict[str, list[str]]) -> list[str]:
    name = called_name(node)
    findings: list[str] = []
    if name in _FORBIDDEN_NAMES or name in CONNECTION_CALLS:
        findings.append(f"line {node.lineno}: calls {name}")
    if name in QUERY_CALLS:
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        findings += [
            f"line {node.lineno}: write Cypher passed to {name}: {text!r}"
            for argument in arguments
            for text in _literal_text(argument, names)
            if WRITE_CYPHER.search(text)
        ]
    return findings


def scan_source(source: str) -> list[str]:
    """Return one finding per write path in `source` (empty: the module is clean).

    A `falkordb` or `redis` import (beyond `redis.exceptions`), a `FalkorDB(...)` construction, an
    import of the gateway's write-capable factory modules, the applier or the Cypher templates,
    `apply_entries`, and write Cypher passed to a query call. An import under `TYPE_CHECKING`
    counts: the walk does not distinguish it, on purpose.
    """
    tree = ast.parse(source)
    names = _text_assignments(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            findings += [
                f"line {node.lineno}: imports {module}"
                for module in _imported_modules(node)
                if _forbidden_import(module)
            ]
        elif isinstance(node, ast.Call):
            findings += _call_findings(node, names)
    return findings


def calls_by_function(source: str) -> dict[str, set[str]]:
    """For each function defined in `source`, the names of the calls in its body."""
    return {
        node.name: {
            name
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            if (name := called_name(call))
        }
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
    }


def reaches(source: str, function: str, target: str) -> bool:
    """Whether `function` calls `target`, directly or through same-module functions."""
    calls = calls_by_function(source)
    seen: set[str] = set()
    pending = [function]
    while pending:
        current = pending.pop()
        if current in seen or current not in calls:
            continue
        seen.add(current)
        if target in calls[current]:
            return True
        pending.extend(calls[current])
    return False
