"""AC-BI-016: every label and relationship type written in Cypher in
`ps-service/src` is in `DOMAIN_SCHEMA` or in a named, justified exception set.

The scanner walks the source with `ast`, so docstrings are skipped and
f-string module constants are resolved. Residual risk (ISSUE "Value"):
dynamic labels / relationship types (unresolvable f-string placeholders) and
property names inside Cypher are not checked; `test_dynamic_site_counts_are_pinned`
makes the dynamic sites impossible to grow silently.
"""

from __future__ import annotations

import ast
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ps_service
from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.vocabulary_exceptions import (
    CELLAR_ELI_NATIVE_LABELS,
    OPERATIONAL_LABELS,
    SYSTEM_MINTED_EDGE_TYPES,
)

_SRC_ROOT = Path(ps_service.__file__).parent
_CYPHER = re.compile(r"\b(?:MATCH|MERGE|CREATE)\b")
_NODE_LABEL = re.compile(r"\(\s*(?:[A-Za-z_]\w*)?\s*:\s*([A-Za-z_]\w*)")
_REL_TYPES = re.compile(r"\[\s*\w*\s*:\s*([A-Z_][A-Z0-9_]*(?:\s*\|\s*:?[A-Z_][A-Z0-9_]*)*)")
_PLACEHOLDER = "\x00"


@dataclass(frozen=True)
class Finding:
    filename: str
    line: int
    name: str
    kind: str


def _module_constants(tree: ast.Module) -> dict[str, str]:
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _docstring_nodes(tree: ast.Module) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _render(node: ast.AST, constants: dict[str, str]) -> tuple[str, int] | None:
    """Return (text, dynamic placeholder count) for a string node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, 0
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    dynamic = 0
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(str(part.value))
        elif isinstance(part, ast.FormattedValue):
            inner = part.value
            if isinstance(inner, ast.Name) and inner.id in constants:
                parts.append(constants[inner.id])
            else:
                parts.append(_PLACEHOLDER)
                dynamic += 1
    return "".join(parts), dynamic


def _names(text: str) -> list[tuple[str, str]]:
    labels = [(name, "label") for name in _NODE_LABEL.findall(text)]
    edges = [
        (rel.strip().lstrip(":"), "edge")
        for group in _REL_TYPES.findall(text)
        for rel in group.split("|")
    ]
    return labels + edges


def _scan(source: str, filename: str) -> tuple[list[Finding], int]:
    """Return every (label | edge) name used in Cypher and the dynamic site count."""
    tree = ast.parse(source)
    constants = _module_constants(tree)
    skipped = _docstring_nodes(tree)
    found: list[Finding] = []
    dynamic_sites = 0
    for node in ast.walk(tree):
        if id(node) in skipped or not isinstance(node, ast.Constant | ast.JoinedStr):
            continue
        rendered = _render(node, constants)
        if rendered is None or not _CYPHER.search(rendered[0]):
            continue
        text, dynamic = rendered
        dynamic_sites += dynamic
        found.extend(Finding(filename, node.lineno, n, k) for n, k in _names(text) if n)
    return found, dynamic_sites


_ALLOWED_LABELS = (
    {node.label for node in DOMAIN_SCHEMA.nodes}
    | set(OPERATIONAL_LABELS)
    | set(CELLAR_ELI_NATIVE_LABELS)
)
_ALLOWED_EDGES = {edge.type for edge in DOMAIN_SCHEMA.edges} | set(SYSTEM_MINTED_EDGE_TYPES)


def scan_cypher_names(source: str, filename: str) -> list[Finding]:
    """Findings for names that are in neither the schema nor a named exception set."""
    found, _ = _scan(source, filename)
    allowed = {"label": _ALLOWED_LABELS, "edge": _ALLOWED_EDGES}
    return [f for f in found if f.name not in allowed[f.kind]]


def _scan_tree(root: Path) -> tuple[list[Finding], Counter[str]]:
    findings: list[Finding] = []
    dynamic: Counter[str] = Counter()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        found, sites = _scan(path.read_text(encoding="utf-8"), relative)
        allowed = {"label": _ALLOWED_LABELS, "edge": _ALLOWED_EDGES}
        findings.extend(f for f in found if f.name not in allowed[f.kind])
        if sites:
            dynamic[relative] = sites
    return findings, dynamic


def _report(findings: list[Finding]) -> str:
    lines = [f"{f.filename}:{f.line} {f.kind} {f.name}" for f in findings]
    return (
        "Cypher names in neither DOMAIN_SCHEMA nor a named exception set:\n"
        + "\n".join(lines)
        + "\nAdd the name to the schema, or to a set in "
        "domain_schema/vocabulary_exceptions.py WITH a justification."
    )


# --- synthetic proofs -------------------------------------------------------


def test_unknown_label_is_flagged() -> None:
    findings = scan_cypher_names('Q = "MATCH (n:Bogus) RETURN n"', "x.py")

    assert [(f.name, f.kind) for f in findings] == [("Bogus", "label")]


def test_exception_label_passes() -> None:
    assert scan_cypher_names('Q = "MATCH (n:PendingReview) RETURN n"', "x.py") == []


def test_docstring_is_ignored() -> None:
    source = 'def f():\n    """MATCH (n:Bogus)-[:NOPE]->(m) is prose."""\n'

    assert scan_cypher_names(source, "x.py") == []


def test_fstring_module_constant_is_resolved() -> None:
    source = 'L = "Bogus"\nQ = f"MATCH (n:{L}) RETURN n"\n'

    assert [f.name for f in scan_cypher_names(source, "x.py")] == ["Bogus"]


def test_dynamic_placeholder_is_not_flagged_but_counted() -> None:
    source = 'def f(label):\n    return f"MATCH (n:{label}) RETURN n"\n'

    assert scan_cypher_names(source, "x.py") == []
    assert _scan(source, "x.py")[1] == 1


def test_relationship_union_is_split() -> None:
    source = 'Q = "MATCH (a:Role)-[r:COVERS|NOPE|OWNS*1..3]->(b) RETURN b"'

    assert [f.name for f in scan_cypher_names(source, "x.py")] == ["NOPE"]


def test_non_cypher_string_is_ignored() -> None:
    assert scan_cypher_names('M = "see (x:Bogus) and [:NOPE]"', "x.py") == []


# --- real tree --------------------------------------------------------------


def test_every_cypher_label_and_relationship_type_is_in_schema_or_a_named_exception() -> None:
    started = time.perf_counter()

    findings, _ = _scan_tree(_SRC_ROOT)

    assert not findings, _report(findings)
    assert time.perf_counter() - started < 5


def test_scan_flags_an_unknown_label_in_a_copied_source_tree(tmp_path: Path) -> None:
    copy = tmp_path / "ps_service"
    shutil.copytree(_SRC_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "planted.py").write_text('Q = "MATCH (n:PlantedBogus) RETURN n"\n', encoding="utf-8")

    findings, _ = _scan_tree(copy)

    assert [(f.filename, f.name) for f in findings] == [("planted.py", "PlantedBogus")]


# Dynamic (unresolvable f-string placeholder) sites in Cypher strings. A change
# here is a conscious edit: a new dynamic site is invisible to the scan.
_DYNAMIC_SITES: dict[str, int] = {
    "company_merge/dedup.py": 2,
    "company_merge/graph_writer.py": 10,
    "domain_mapper/graph_writer.py": 6,
    "export/embeddings.py": 3,
    "export/serialize.py": 2,
    "ingestion/adapters/internal_seed/persist.py": 7,
    "ingestion/graph_writer.py": 5,
    "policy_lifecycle/graph_writer.py": 3,
    "restore/populate.py": 4,
}


def test_dynamic_site_counts_are_pinned() -> None:
    _, dynamic = _scan_tree(_SRC_ROOT)

    assert dict(dynamic) == _DYNAMIC_SITES
