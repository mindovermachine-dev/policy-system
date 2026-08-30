"""AC-BI-004: ps-cli must only ever reach PS Service over REST, never a direct dependency.

This is a structural, AST-based guard (not a regex) that walks every module under
``ps-cli/src/ps_cli`` and asserts none of them imports a data-store client, an LLM
client, or any ``ps_service`` module directly.
"""

import ast
from pathlib import Path

_FORBIDDEN_MODULES = frozenset({"falkordb", "litellm", "graphrag_sdk"})

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ps_cli"


def _iter_source_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _is_forbidden(module_name: str) -> bool:
    top_level = module_name.split(".", maxsplit=1)[0]
    return top_level in _FORBIDDEN_MODULES or top_level == "ps_service"


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def test_no_module_imports_a_forbidden_infrastructure_dependency() -> None:
    """No file under ps_cli/**/*.py imports falkordb, litellm, graphrag_sdk, or ps_service.*."""
    violations: list[str] = []
    for source_file in _iter_source_files():
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for module_name in _imported_module_names(tree):
            if _is_forbidden(module_name):
                violations.append(f"{source_file}: imports '{module_name}'")

    assert not violations, "Forbidden direct infrastructure imports found:\n" + "\n".join(
        violations
    )


def test_source_files_were_actually_discovered() -> None:
    """Guard against the walk silently finding zero files (a rename/path-typo trap)."""
    assert _iter_source_files(), f"No .py files found under {_SRC_ROOT}"
