"""AC-BI-017 (issue #130): `audit` and `persistence` stay component-independent.

`audit` is a shared component that consumers depend on, never the reverse; `persistence`
is shared plumbing that every state-store component may import but that itself imports
none of them. Checked structurally on the real source tree via `ast` (imports) and a
plain text scan (prose), so a docstring cannot quietly re-introduce the coupling.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.audit as audit_package
import ps_service.persistence as persistence_package

_CONSUMERS = ("authz", "runtime_config", "curated_source", "invitations")


def _package_sources(package: object) -> list[Path]:
    package_file = getattr(package, "__file__", None)
    assert package_file is not None
    return sorted(Path(package_file).parent.rglob("*.py"))


def _imported_modules(source: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _forbidden_imports(package: object, forbidden: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for source in _package_sources(package):
        for module in _imported_modules(source):
            if any(
                module == f"ps_service.{name}" or module.startswith(f"ps_service.{name}.")
                for name in forbidden
            ):
                hits.append(f"{source.name}: {module}")
    return hits


def test_audit_package_imports_nothing_from_authz() -> None:
    """`ps_service.audit` imports no consumer component (authz, runtime_config, ...)."""
    assert _forbidden_imports(audit_package, _CONSUMERS) == []


def test_audit_package_source_mentions_no_consumer_component_by_name() -> None:
    """Not even in prose: a naive `grep authz` over `audit/` stays clean."""
    mentions = [
        source.name
        for source in _package_sources(audit_package)
        if "authz" in source.read_text(encoding="utf-8").lower()
    ]

    assert mentions == []


def test_persistence_package_imports_none_of_the_components_that_use_it() -> None:
    """`ps_service.persistence` may be imported by components; it imports none of them."""
    assert _forbidden_imports(persistence_package, ("audit", *_CONSUMERS)) == []
