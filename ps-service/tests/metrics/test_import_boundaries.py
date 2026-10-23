"""Guard tests for the import boundaries around `ps_service.metrics`."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVICE_SRC = _REPO_ROOT / "ps-service" / "src" / "ps_service"
_METRICS_SRC = _SERVICE_SRC / "metrics"
_PROMETHEUS_CLIENT_MODULES = frozenset({"ps_service/metrics/exposition.py"})
_FEATURE_PREFIXES = (
    "ps_service.api",
    "ps_service.graph_gateway",
    "ps_service.ingestion",
    "ps_service.main",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _is_module(imported: str, root: str) -> bool:
    return imported == root or imported.startswith(f"{root}.")


def _service_files() -> list[Path]:
    return sorted(_SERVICE_SRC.rglob("*.py"))


def test_metrics_package_exists() -> None:
    assert (_METRICS_SRC / "__init__.py").is_file()


def test_prometheus_client_is_imported_only_by_the_exposition_module() -> None:
    offenders = [
        path.relative_to(_SERVICE_SRC.parent).as_posix()
        for path in _service_files()
        if any(_is_module(name, "prometheus_client") for name in _imported_modules(path))
    ]
    unexpected = [o for o in offenders if o not in _PROMETHEUS_CLIENT_MODULES]

    assert unexpected == []


def test_otel_sdk_is_imported_only_inside_the_metrics_package() -> None:
    offenders = [
        path.relative_to(_SERVICE_SRC).as_posix()
        for path in _service_files()
        if not path.is_relative_to(_METRICS_SRC)
        and any(_is_module(name, "opentelemetry.sdk") for name in _imported_modules(path))
    ]

    assert offenders == []


def test_metrics_package_imports_no_feature_package() -> None:
    offenders = sorted(
        f"{path.relative_to(_SERVICE_SRC).as_posix()} -> {name}"
        for path in _METRICS_SRC.rglob("*.py")
        for name in _imported_modules(path)
        if any(_is_module(name, prefix) for prefix in _FEATURE_PREFIXES)
    )

    assert offenders == []


def test_import_linter_treats_metrics_as_a_leaf_foundation() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {c["id"]: c for c in config["tool"]["importlinter"]["contracts"]}

    assert "ps_service.metrics" in contracts["leaf-foundations"]["source_modules"]


def test_prometheus_client_is_a_pinned_direct_dependency() -> None:
    config = tomllib.loads((_REPO_ROOT / "ps-service" / "pyproject.toml").read_text("utf-8"))
    dependencies = config["project"]["dependencies"]

    assert any(dep.startswith("prometheus-client==") for dep in dependencies)
    assert any(dep.startswith("opentelemetry-sdk==") for dep in dependencies)
    assert any(dep.startswith("opentelemetry-exporter-prometheus==") for dep in dependencies)
