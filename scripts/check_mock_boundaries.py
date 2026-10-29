"""AST-based guardrail for `unittest.mock`/`monkeypatch` boundary discipline.

Walks Python test files and flags two kinds of violations of the Detroit-style testing standard
(mock only the approved external boundaries):

1. A `mock.patch(...)`/`@patch(...)`/`mock.patch.object(...)`/`monkeypatch.setattr(...)` call
   or decorator whose resolved dotted target is not listed in
   `docs/coding-standards/approved-mock-boundaries.yaml` and carries no
   `# detroit-exception: <reason>` suppression comment.
2. A bare `Mock()`/`MagicMock()`/`AsyncMock()` construction that is not directly consumed (or,
   within the same function, traced via a simple variable) by an already-approved-boundary
   `patch`/`setattr` call.

Paths may be files or directories (searched recursively); with none given it scans
`ps-service/tests` and `ps-cli/tests`. `--boundary-file` overrides the approved-boundary YAML.
Each violation prints as `<file>:<line>: <message>`; the exit status is 1 when any violation is
found and 0 otherwise. The check runs over the whole tree in the `.insitu.yml` `trunk-worthy`
wave; run it by hand with
`uv run python scripts/check_mock_boundaries.py [paths...]`.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_BOUNDARY_FILE = Path("docs/coding-standards/approved-mock-boundaries.yaml")
DEFAULT_SCAN_ROOTS = ("ps-service/tests", "ps-cli/tests")
_EXCEPTION_MARKER = "# detroit-exception:"
_MOCK_CONSTRUCTOR_NAMES = frozenset({"Mock", "MagicMock", "AsyncMock"})
_VALUE_KEYWORDS = frozenset({"new", "new_callable", "value"})
_TWO_ARGS = 2
_THREE_ARGS = 3


@dataclass(frozen=True, slots=True)
class Violation:
    """A single disallowed mock-boundary or bare-construction site."""

    file: str
    line: int
    message: str

    def format(self) -> str:
        """Render the violation in the output shape every consumer parses.

        Returns:
            The text `<file>:<line>: <message>`.
        """
        return f"{self.file}:{self.line}: {self.message}"


def load_boundary_entries(yaml_path: Path) -> frozenset[str]:
    """Load the flat set of approved dotted-path targets from a boundary YAML file.

    The file groups entries under category keys (`falkordb`, `postgres`, ...); this flattens
    every string list value into one comparison set. Non-string/non-list content is ignored.

    Args:
        yaml_path: Path of the approved-boundaries YAML file.

    Returns:
        The set of approved dotted-path targets; empty when the file's top level is not a mapping.

    Raises:
        OSError: The file cannot be read.
        yaml.YAMLError: The file is not valid YAML.
    """
    raw_text = yaml_path.read_text(encoding="utf-8")
    loaded = cast("object", yaml.safe_load(raw_text))
    if not isinstance(loaded, dict):
        return frozenset()
    loaded_dict = cast("dict[object, object]", loaded)
    entries: set[str] = set()
    for value in loaded_dict.values():
        if not isinstance(value, list):
            continue
        value_list = cast("list[object]", value)
        entries.update(item for item in value_list if isinstance(item, str))
    return frozenset(entries)


def _str_const_value(node: ast.expr) -> str | None:
    """Return the literal string value of `node`, or `None` if it is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _classify_call(call: ast.Call) -> str | None:
    """Classify a call as a "patch", "patch_object", or "setattr" target-bearing shape.

    Returns `None` for calls that are not one of `mock.patch(...)`, `mock.patch.object(...)`,
    or `monkeypatch.setattr(...)` (matched by the callee's trailing dotted component(s), which
    also covers aliased imports).

    Requires at least two dotted components for the "setattr" shape (`<something>.setattr(...)`)
    so the bare builtin `setattr(obj, attr, value)` -- used, for example, by frozen-dataclass
    immutability tests to bypass the type checker's static attribute check and unrelated to
    mocking -- is never taken for a mock-boundary call. Misclassifying it would demand a
    `# detroit-exception:` comment on ordinary non-mock test code, because an unresolvable
    target is a violation.

    Args:
        call: The call node to classify.

    Returns:
        `"patch"`, `"patch_object"` or `"setattr"`, or `None` when the call is none of them.
    """
    try:
        callee = ast.unparse(call.func)
    except ValueError:  # pragma: no cover - defensive; ast.unparse rarely fails
        return None
    parts = callee.split(".")
    if len(parts) >= _TWO_ARGS and parts[-1] == "setattr":
        return "setattr"
    if len(parts) >= _TWO_ARGS and parts[-2] == "patch" and parts[-1] == "object":
        return "patch_object"
    if parts[-1] == "patch":
        return "patch"
    return None


def _resolve_patch_target(call: ast.Call) -> str | None:
    """Resolve `mock.patch(...)`/`@patch(...)`: first positional or `target=`/`new=` literal."""
    if call.args:
        return _str_const_value(call.args[0])
    for kw in call.keywords:
        if kw.arg in {"target", "new"}:
            resolved = _str_const_value(kw.value)
            if resolved is not None:
                return resolved
    return None


def _resolve_patch_object_target(call: ast.Call) -> str | None:
    """Resolve `mock.patch.object(target_expr, "attr", ...)` via `unparse(target_expr).attr`."""
    if len(call.args) < _TWO_ARGS:
        return None
    attr = _str_const_value(call.args[1])
    if attr is None:
        return None
    return f"{ast.unparse(call.args[0])}.{attr}"


def _resolve_setattr_target(call: ast.Call) -> str | None:
    """Resolve `monkeypatch.setattr(...)`'s 2-arg (string) or 3-arg (expr, attr) shape."""
    if not call.args:
        return None
    first_as_str = _str_const_value(call.args[0])
    if first_as_str is not None:
        return first_as_str
    if len(call.args) >= _TWO_ARGS:
        attr = _str_const_value(call.args[1])
        if attr is not None:
            return f"{ast.unparse(call.args[0])}.{attr}"
    return None


def _resolve_target(call: ast.Call, kind: str) -> str | None:
    """Reconstruct the dotted-path target string a classified call resolves to, if any."""
    if kind == "patch":
        return _resolve_patch_target(call)
    if kind == "patch_object":
        return _resolve_patch_object_target(call)
    return _resolve_setattr_target(call)


def _value_candidate_exprs(call: ast.Call, kind: str) -> list[ast.expr]:
    """Return the expression node(s) occupying the "replacement value" position of `call`."""
    candidates: list[ast.expr] = []
    if kind == "patch" and len(call.args) >= _TWO_ARGS:
        candidates.append(call.args[1])
    elif kind == "patch_object" and len(call.args) >= _THREE_ARGS:
        candidates.append(call.args[2])
    elif kind == "setattr":
        is_two_arg_form = (
            call.args and _str_const_value(call.args[0]) is not None and len(call.args) >= _TWO_ARGS
        )
        if is_two_arg_form:
            candidates.append(call.args[1])
        elif len(call.args) >= _THREE_ARGS:
            candidates.append(call.args[2])
    candidates.extend(kw.value for kw in call.keywords if kw.arg in _VALUE_KEYWORDS)
    return candidates


def _suffix_matches(resolved: str, boundary_entries: frozenset[str]) -> bool:
    """Suffix-match `resolved` against the boundary set.

    Tries the full dotted string first (exact/precise match), then progressively shorter
    trailing-component suffixes down to the final attribute/function name alone. This is the
    deliberate trade-off: perfect static resolution of
    aliased imports (`from ps_service.restore import staging as s`) is out of scope, so a short
    aliased target (`s.stage_graph`) is reconciled against a fully-qualified boundary-list entry
    (`ps_service.restore.staging.stage_graph`) by matching on the trailing function name.
    """
    parts = resolved.split(".")
    for k in range(len(parts), 0, -1):
        candidate = ".".join(parts[-k:])
        for entry in boundary_entries:
            if entry == candidate or entry.endswith(f".{candidate}"):
                return True
    return False


def _exception_comment_present(source_lines: list[str], lineno: int) -> bool:
    """True if `# detroit-exception:` appears on `lineno` or above it, in its comment block.

    Walking the whole contiguous comment block (not just the single line directly above)
    covers a multi-line explanatory comment whose `# detroit-exception:` marker line isn't
    the last one before the call, such as an explanatory comment whose marker is its first
    line, several lines above the `monkeypatch.setattr(...)` call it documents.

    Args:
        source_lines: The module's source split into lines.
        lineno: One-based line number of the call.

    Returns:
        Whether the marker is on the call's line or in the comment block directly above it.
    """
    in_range = 1 <= lineno <= len(source_lines)
    if in_range and _EXCEPTION_MARKER in source_lines[lineno - 1]:
        return True
    candidate_lineno = lineno - 1
    while 1 <= candidate_lineno <= len(source_lines):
        line = source_lines[candidate_lineno - 1]
        if not line.strip().startswith("#"):
            break
        if _EXCEPTION_MARKER in line:
            return True
        candidate_lineno -= 1
    return False


class _ParentAnnotator(ast.NodeVisitor):
    """Single-pass visitor that records each node's direct parent for upward AST walks."""

    def __init__(self) -> None:
        self.parents: dict[ast.AST, ast.AST] = {}

    def generic_visit(self, node: ast.AST) -> None:
        """Record `node` as the parent of each of its direct children, then recurse."""
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        super().generic_visit(node)


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Walk `parents` upward from `node` to find its nearest enclosing function, if any."""
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parents.get(current)
    return None


@dataclass(frozen=True, slots=True)
class _TargetSite:
    """A classified, resolved `patch`/`setattr` call site found in one pass over a module."""

    call: ast.Call
    kind: str
    resolved: str | None


def _find_target_sites(tree: ast.Module) -> list[_TargetSite]:
    """Find and classify every `patch`/`patch.object`/`setattr`-shaped call in `tree`."""
    sites: list[_TargetSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _classify_call(node)
        if kind is None:
            continue
        sites.append(_TargetSite(call=node, kind=kind, resolved=_resolve_target(node, kind)))
    return sites


def _target_violations(
    sites: list[_TargetSite],
    boundary_entries: frozenset[str],
    source_lines: list[str],
    relative_path: str,
) -> list[Violation]:
    """Flag any patch/setattr target that is neither approved nor exception-marked.

    A target that cannot be statically resolved to a dotted-path string at all (an f-string
    `ast.JoinedStr`, or any other dynamic expression `_resolve_target` can't reconstruct) is
    NOT silently treated as compliant: it must still carry an explicit
    `# detroit-exception:` comment, exactly like a resolvable-but-disallowed target, or it is
    flagged.
    """
    violations: list[Violation] = []
    for site in sites:
        if _exception_comment_present(source_lines, site.call.lineno):
            continue
        if site.resolved is None:
            message = (
                "unresolvable dynamic mock target -- add # detroit-exception: <reason> "
                "if intentional"
            )
            violations.append(Violation(file=relative_path, line=site.call.lineno, message=message))
            continue
        if _suffix_matches(site.resolved, boundary_entries):
            continue
        message = (
            f"disallowed mock target '{site.resolved}' "
            "(not in approved-mock-boundaries.yaml, no # detroit-exception comment)"
        )
        violations.append(Violation(file=relative_path, line=site.call.lineno, message=message))
    return violations


def _is_mock_constructor_call(call: ast.Call) -> bool:
    """True if `call`'s callee's trailing component is `Mock`/`MagicMock`/`AsyncMock`."""
    try:
        callee = ast.unparse(call.func)
    except ValueError:  # pragma: no cover - defensive; ast.unparse rarely fails
        return False
    return callee.split(".")[-1] in _MOCK_CONSTRUCTOR_NAMES


def _site_is_compliant(site: _TargetSite, boundary_entries: frozenset[str]) -> bool:
    """True if a target site's own resolved target is on the approved boundary list."""
    return site.resolved is not None and _suffix_matches(site.resolved, boundary_entries)


def _immediate_use_exempts(
    ctor_call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    target_sites_by_call: dict[int, _TargetSite],
    boundary_entries: frozenset[str],
) -> bool:
    """True if a mock construction is passed directly to an approved-target call."""
    parent = parents.get(ctor_call)
    if not isinstance(parent, ast.Call):
        return False
    site = target_sites_by_call.get(id(parent))
    if site is None or not _site_is_compliant(site, boundary_entries):
        return False
    return any(candidate is ctor_call for candidate in _value_candidate_exprs(parent, site.kind))


def _same_function_trace_exempts(
    ctor_call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    all_sites: list[_TargetSite],
    boundary_entries: frozenset[str],
) -> bool:
    """True if `var = Mock()` is later passed to an approved `setattr`/`patch` in its function."""
    assign = parents.get(ctor_call)
    if not isinstance(assign, ast.Assign | ast.AnnAssign):
        return False
    targets = assign.targets if isinstance(assign, ast.Assign) else [assign.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        return False
    var_name = targets[0].id
    function_node = _enclosing_function(ctor_call, parents)
    if function_node is None:
        return False
    later_sites = (
        site
        for site in all_sites
        if site.call.lineno > ctor_call.lineno and _node_within(site.call, function_node, parents)
    )
    for site in later_sites:
        if not _site_is_compliant(site, boundary_entries):
            continue
        candidates = _value_candidate_exprs(site.call, site.kind)
        if any(isinstance(c, ast.Name) and c.id == var_name for c in candidates):
            return True
    return False


def _node_within(node: ast.AST, ancestor: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if `ancestor` is `node` or one of its transitive parents."""
    current: ast.AST | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = parents.get(current)
    return False


def _construction_violations(
    tree: ast.Module,
    all_sites: list[_TargetSite],
    boundary_entries: frozenset[str],
    source_lines: list[str],
    relative_path: str,
) -> list[Violation]:
    """Flag bare `Mock()`/`MagicMock()`/`AsyncMock()` constructions that nothing approves."""
    annotator = _ParentAnnotator()
    annotator.visit(tree)
    parents = annotator.parents
    target_sites_by_call = {id(site.call): site for site in all_sites}

    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_mock_constructor_call(node):
            continue
        if _immediate_use_exempts(node, parents, target_sites_by_call, boundary_entries):
            continue
        if _same_function_trace_exempts(node, parents, all_sites, boundary_entries):
            continue
        if _exception_comment_present(source_lines, node.lineno):
            continue
        ctor_name = ast.unparse(node.func).split(".")[-1]
        message = (
            f"disallowed bare '{ctor_name}(' construction (no mock.patch/monkeypatch.setattr "
            "target resolves to approved-mock-boundaries.yaml; construct via a real collaborator "
            "or an approved-boundary fake, or add # detroit-exception: <reason>)"
        )
        violations.append(Violation(file=relative_path, line=node.lineno, message=message))
    return violations


def scan_source(
    source: str, relative_path: str, boundary_entries: frozenset[str]
) -> list[Violation]:
    """Scan one module's source text for disallowed mock-boundary and bare-construction sites.

    Args:
        source: Python source text of a test module.
        relative_path: Path shown in each violation and used as the parse filename.
        boundary_entries: Approved dotted-path targets, as `load_boundary_entries` returns.

    Returns:
        The violations ordered by line number; empty when the module is clean.

    Raises:
        SyntaxError: `source` is not valid Python.
    """
    tree = ast.parse(source, filename=relative_path)
    source_lines = source.splitlines()
    sites = _find_target_sites(tree)
    violations = _target_violations(sites, boundary_entries, source_lines, relative_path)
    violations.extend(
        _construction_violations(tree, sites, boundary_entries, source_lines, relative_path)
    )
    return sorted(violations, key=lambda v: v.line)


def _scan_path(path: Path, boundary_entries: frozenset[str]) -> list[Violation]:
    """Scan a single file or every `.py` file under a directory, recursively."""
    violations: list[Violation] = []
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file_path in files:
        source = file_path.read_text(encoding="utf-8")
        violations.extend(scan_source(source, str(file_path), boundary_entries))
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guardrail over the given paths (default: `ps-service/tests`, `ps-cli/tests`).

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
            Positional paths are files or directories; `--boundary-file` overrides the
            approved-boundary YAML.

    Returns:
        1 if any violation was found, otherwise 0.

    Raises:
        OSError: The boundary file or a scanned file cannot be read.
        yaml.YAMLError: The boundary file is not valid YAML.
        SyntaxError: A scanned file is not valid Python.

    Side Effects:
        Prints one line per violation (`<file>:<line>: <message>`) to stdout.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_SCAN_ROOTS))
    parser.add_argument("--boundary-file", type=Path, default=DEFAULT_BOUNDARY_FILE)
    args = parser.parse_args(argv)
    paths = cast("list[str]", args.paths)
    boundary_file = cast("Path", args.boundary_file)

    boundary_entries = load_boundary_entries(boundary_file)
    violations: list[Violation] = []
    for raw_path in paths:
        violations.extend(_scan_path(Path(raw_path), boundary_entries))
    violations.sort(key=lambda v: (v.file, v.line))

    for violation in violations:
        sys.stdout.write(f"{violation.format()}\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
