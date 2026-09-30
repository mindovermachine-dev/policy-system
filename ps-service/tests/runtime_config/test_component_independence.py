"""AC-BI-017/018 (issue #130): `runtime_config` and `authz` write audit rows only via `AuditStore`.

Structural checks on the real source tree via `ast` and a plain text scan, so a later edit
cannot quietly add a config-specific hook to `audit`, a raw `INSERT INTO audit_events`, or an
IdP-specific reference to the components that must survive an IdP swap.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service.audit as audit_package
import ps_service.authz as authz_package
import ps_service.runtime_config as runtime_config_package

_ALLOWED_AUDIT_MODULES = frozenset(
    {
        "ps_service.audit",
        "ps_service.audit.models",
        "ps_service.audit.errors",
        "ps_service.audit.store",  # only the public `AuditStore`/`PsycopgAuditStore`, see below
    }
)
_PUBLIC_AUDIT_STORE_NAMES = frozenset({"AuditStore", "PsycopgAuditStore"})
_IDP_TERMS = ("authentik", "invitations")


def _sources(package: object) -> list[Path]:
    package_file = getattr(package, "__file__", None)
    assert package_file is not None
    return sorted(Path(package_file).parent.rglob("*"))


def _python_sources(package: object) -> list[Path]:
    return [path for path in _sources(package) if path.suffix == ".py"]


def _audit_imports(source: Path) -> list[tuple[str, str | None]]:
    """`(module, imported name)` for every import of `ps_service.audit*` in `source`."""
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(
                (alias.name, None)
                for alias in node.names
                if alias.name.startswith("ps_service.audit")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "ps_service.audit" or node.module.startswith("ps_service.audit."))
        ):
            found.extend((node.module, alias.name) for alias in node.names)
    return found


def test_authz_and_runtime_config_write_audit_only_via_audit_store_record() -> None:
    offenders: list[str] = []
    for package in (authz_package, runtime_config_package):
        for source in _python_sources(package):
            text = source.read_text(encoding="utf-8")
            if "INSERT INTO audit_events" in text:
                offenders.append(f"{source.name}: writes audit_events directly")
            for module, name in _audit_imports(source):
                if module not in _ALLOWED_AUDIT_MODULES:
                    offenders.append(f"{source.name}: imports {module}")
                elif module == "ps_service.audit.store" and name not in _PUBLIC_AUDIT_STORE_NAMES:
                    offenders.append(f"{source.name}: imports private {module}.{name}")

    assert offenders == []


def test_runtime_config_imports_no_consumer_of_itself() -> None:
    forbidden = ("ps_service.curated_source", "ps_service.authz", "ps_service.invitations")
    offenders = [
        f"{source.name}: {node.module}"
        for source in _python_sources(runtime_config_package)
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith(forbidden)
    ]

    assert offenders == []


def test_audit_authz_runtime_config_reference_no_idp_specific_names() -> None:
    offenders = [
        f"{source.name}: {term}"
        for package in (audit_package, authz_package, runtime_config_package)
        for source in _sources(package)
        if source.suffix in {".py", ".sql"}
        for term in _IDP_TERMS
        if term in source.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []
