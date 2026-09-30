"""AST-based guardrail for `unittest.mock`/`monkeypatch` boundary discipline (AC-BI-009).

Walks Python test files and flags two kinds of Detroit-style-standard violations, per
`.orchestrator/tracker/issue-163/PLAN.md` §4.2 and its Appendix A amendment
(`.orchestrator/tracker/issue-163/CHANGES.md`):

1. A `mock.patch(...)`/`@patch(...)`/`mock.patch.object(...)`/`monkeypatch.setattr(...)` call
   or decorator whose resolved dotted target is not listed in
   `docs/coding-standards/approved-mock-boundaries.yaml` and carries no
   `# detroit-exception: <reason>` suppression comment.
2. A bare `Mock()`/`MagicMock()`/`AsyncMock()` construction that is not directly consumed (or,
   within the same function, traced via a simple variable) by an already-approved-boundary
   `patch`/`setattr` call.

Wired into `.insitu.yml`'s `trunk-worthy` wave (unscoped, whole-tree invocation) and into
`.pre-commit-config.yaml` as a staged-file-scoped local hook (issue #163 Slice Q) -- it can also
still be invoked manually via `uv run python scripts/check_mock_boundaries.py [paths...]`.
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
        """Render as `<file>:<line>: <message>`, AC-BI-009's required output shape."""
        return f"{self.file}:{self.line}: {self.message}"


def load_boundary_entries(yaml_path: Path) -> frozenset[str]:
    """Load the flat set of approved dotted-path targets from a boundary YAML file.

    The file groups entries under category keys (`falkordb`, `postgres`, ...); this flattens
    every string list value into one comparison set. Non-string/non-list content is ignored.
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
    or `monkeypatch.setattr(...)` (matched by the callee's trailing dotted component(s), the
    same heuristic PLAN.md §4.2 point 2 uses for aliased-import resolution).

    Requires at least two dotted components for the "setattr" shape (`<something>.setattr(...)`)
    so the bare builtin `setattr(obj, attr, value)` -- used by e.g. frozen-dataclass immutability
    tests (`test_models.py`'s `_mutate` helper) to bypass basedpyright's static attribute check,
    nothing to do with mocking -- is never misclassified as a mock-boundary call site (a Gap 1
    interstitial-fix regression risk: it resolves to `None`, and after Gap 1's fix `None` is no
    longer silently accepted, so a bare-`setattr` misclassification would wrongly demand a
    `# detroit-exception:` comment on ordinary non-mock test code).
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
    deliberate, documented trade-off from PLAN.md §4.2 point 2: perfect static resolution of
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
    the last one before the call -- e.g. `test_signing_hardening.py`'s Slice L comment, whose
    marker is its first line, 6 lines above the `monkeypatch.setattr(...)` call it documents.
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
    """§4.2 points 1-4: flag any resolved target that is neither approved nor exception-marked.

    A target that cannot be statically resolved to a dotted-path string at all (an f-string
    `ast.JoinedStr`, or any other dynamic expression `_resolve_target` can't reconstruct) is
    NOT silently treated as compliant -- Gap 1 (issue #163 Slice L / interstitial fix): it must
    still carry an explicit `# detroit-exception:` comment, exactly like a resolvable-but-
    disallowed target, or it is flagged.
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
    """Appendix A point 2: exempt a construction directly passed to an approved-target call."""
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
    """Appendix A point 3: exempt `var = Mock()` traced to a later approved `setattr`/`patch`."""
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
    """Appendix A: flag bare `Mock()`/`MagicMock()`/`AsyncMock()` constructions."""
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
    """Scan one module's source text for disallowed mock-boundary and bare-construction sites."""
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

    Prints one line per violation (`<file>:<line>: <message>`) and returns a non-zero exit
    status if any violation was found.
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
