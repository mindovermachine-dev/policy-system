"""`ingestion_runs` writes audit rows only via `AuditStore`.

Mirrors `tests/runtime_config/test_component_independence.py`: structural checks on the real
source tree, so a later edit cannot quietly add a raw `INSERT INTO audit_events` or reach into
`audit`'s private modules.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import ps_service.ingestion_runs as ingestion_runs_package

_ALLOWED_AUDIT_MODULES = frozenset(
    {
        "ps_service.audit",
        "ps_service.audit.models",
        "ps_service.audit.errors",
        "ps_service.audit.store",  # only the public `AuditStore`/`PsycopgAuditStore`, see below
    }
)
_PUBLIC_AUDIT_STORE_NAMES = frozenset({"AuditStore", "PsycopgAuditStore"})
_RAW_AUDIT_INSERT = re.compile(r"INSERT\s+INTO\s+(audit_log\.)?audit_events\b", re.IGNORECASE)
"""A raw write to the audit table, qualified or not (the table lives in `audit_log`)."""


def _python_sources() -> list[Path]:
    package_file = ingestion_runs_package.__file__
    assert package_file is not None
    return sorted(Path(package_file).parent.rglob("*.py"))


def _audit_imports(source: Path) -> list[tuple[str, str | None]]:
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


def test_ingestion_runs_writes_audit_only_via_the_public_audit_store() -> None:
    offenders: list[str] = []
    for source in _python_sources():
        text = source.read_text(encoding="utf-8")
        if _RAW_AUDIT_INSERT.search(text):
            offenders.append(f"{source.name}: writes audit_events directly")
        for module, name in _audit_imports(source):
            if module not in _ALLOWED_AUDIT_MODULES:
                offenders.append(f"{source.name}: imports {module}")
            elif module == "ps_service.audit.store" and name not in _PUBLIC_AUDIT_STORE_NAMES:
                offenders.append(f"{source.name}: imports private {module}.{name}")

    assert offenders == []


def test_raw_audit_insert_pattern_catches_both_spellings_of_the_table() -> None:
    assert _RAW_AUDIT_INSERT.search("INSERT INTO audit_events (id)")
    assert _RAW_AUDIT_INSERT.search("insert   into audit_log.audit_events (id)")
    assert not _RAW_AUDIT_INSERT.search("INSERT INTO audit_events_archive (id)")
