"""The AST scan behind the "no write path to FalkorDB" guards (issues #212 and #213).

Every component whose graph writes were routed through the Graph Write Gateway has a guard test
that scans its package with `scan_source`: no module may open a FalkorDB connection of its own,
name the gateway's apply machinery, or pass write Cypher to a graph handle. The scan is an AST
walk. It looks at the text handed to a call named `query`, `ro_query`, `_execute_query`,
`_read_query`, `read_rows` or `query_rows`: a string literal, the constant parts of an f-string, or
a name that resolves to such text. It never looks at docstrings or prose, so a module may
*describe* `MERGE` without being flagged, and it matches the uppercase keywords only, so the
lowercase `'merged'` of a read stays clean.

The connection-helper module of a package (the one place that opens the READ handle) is scanned
with `allow_connection=True`: that admits the connection calls and the `falkordb` and `redis`
imports it needs, and nothing else. Write Cypher, the applier and cypher imports and
`apply_entries` are never admitted.

`reaches` is the positive half: whether a function calls a target directly or through same-module
functions (by name, no mock), so a guard can prove every writer ends at the gateway sink.
"""

from __future__ import annotations

import ast
import re

__all__ = ["called_name", "calls_by_function", "reaches", "scan_source"]

QUERY_CALLS = frozenset(
    {"query", "ro_query", "_execute_query", "_read_query", "read_rows", "query_rows"}
)
CONNECTION_CALLS = frozenset({"FalkorDB", "connect", "connect_from_config", "select_graph"})
WRITE_CYPHER = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|DROP|DETACH|FOREACH)\b")
_ALWAYS_FORBIDDEN_IMPORT_PREFIXES = (
    "ps_service.graph_gateway.applier",
    "ps_service.graph_gateway.cypher",
)
_CONNECTION_IMPORT_PREFIXES = ("falkordb", "redis")
_ALLOWED_REDIS_PREFIX = "redis.exceptions"
_FORBIDDEN_NAMES = frozenset({"apply_entries"})


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


def _is_allowed_import(node: ast.ImportFrom, allowed: frozenset[str]) -> bool:
    """Whether every name `node` imports is one of the `allowed` fully qualified names."""
    if node.module is None or node.level != 0:
        return False
    return all(f"{node.module}.{alias.name}" in allowed for alias in node.names)


def _forbidden_import(module: str, *, allow_connection: bool) -> bool:
    if module.startswith(_ALWAYS_FORBIDDEN_IMPORT_PREFIXES):
        return True
    if allow_connection:
        return False
    if module.startswith("redis"):
        return not module.startswith(_ALLOWED_REDIS_PREFIX)
    return module.startswith(_CONNECTION_IMPORT_PREFIXES)


def _call_findings(
    node: ast.Call, names: dict[str, list[str]], *, allow_connection: bool
) -> list[str]:
    name = called_name(node)
    findings: list[str] = []
    if name in _FORBIDDEN_NAMES or (name in CONNECTION_CALLS and not allow_connection):
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


def scan_source(
    source: str, *, allow_connection: bool = False, allow_imports: frozenset[str] = frozenset()
) -> list[str]:
    """Return one finding per write path in `source` (empty: the module is clean).

    `allow_connection` admits the connection calls and the `falkordb` / `redis` imports of a
    package's one connection-helper module; write Cypher and the other forbidden names are never
    admitted. `allow_imports` admits individual `from module import name` statements by their
    fully qualified names (`module.name`), for a module that needs one read-side constant from a
    forbidden module (never a template or the apply machinery: the caller names it and says why).
    """
    tree = ast.parse(source)
    names = _text_assignments(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_allowed_import(node, allow_imports):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            findings += [
                f"line {node.lineno}: imports {module}"
                for module in _imported_modules(node)
                if _forbidden_import(module, allow_connection=allow_connection)
            ]
        elif isinstance(node, ast.Call):
            findings += _call_findings(node, names, allow_connection=allow_connection)
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
