"""Tests for `ps_service.state_migrations`, the one shared list of PS state migration sources.

Service startup and the provisioning CLI must apply the same ordinary
component migrations in the same order, so the list lives in exactly one module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import ps_service
from ps_service import main as main_module
from ps_service import state_migrations
from ps_service.audit import PRIVILEGED_MIGRATIONS_DIR as AUDIT_PRIVILEGED_MIGRATIONS_DIR
from ps_service.graph_gateway import MIGRATIONS_DIR as GRAPH_GATEWAY_MIGRATIONS_DIR
from ps_service.state_migrations import (
    AUDIT_INTEGRITY_MIGRATION_SOURCE,
    GRAPH_GATEWAY_MIGRATION_SOURCE,
    ORDINARY_STATE_MIGRATION_SOURCES,
)

# Components whose migrations do not belong in the ordinary ps_state list: passkey_signing owns a
# separate database; graph_gateway is applied by the privileged runner.
_NOT_ORDINARY = {"passkey_signing", "graph_gateway"}


def test_ordinary_sources_cover_every_stateful_component_migrations_dir() -> None:
    package_dir = Path(ps_service.__file__).parent
    on_disk = {
        path.parent.name
        for path in package_dir.glob("*/migrations")
        if path.is_dir() and path.parent.name not in _NOT_ORDINARY
    }

    listed = {source.component for source in ORDINARY_STATE_MIGRATION_SOURCES}

    assert listed == on_disk
    for source in ORDINARY_STATE_MIGRATION_SOURCES:
        assert source.directory == package_dir / source.component / "migrations"


def test_ordinary_sources_keep_the_startup_order() -> None:
    assert [source.component for source in ORDINARY_STATE_MIGRATION_SOURCES] == [
        "audit",
        "authz",
        "runtime_config",
        "ingestion_runs",
    ]


def test_graph_gateway_source_is_separate_from_the_ordinary_sources() -> None:
    assert GRAPH_GATEWAY_MIGRATION_SOURCE.component == "graph_gateway"
    assert GRAPH_GATEWAY_MIGRATION_SOURCE.directory == GRAPH_GATEWAY_MIGRATIONS_DIR
    assert GRAPH_GATEWAY_MIGRATION_SOURCE not in ORDINARY_STATE_MIGRATION_SOURCES


def test_audit_integrity_source_is_privileged_and_separate_from_the_other_sources() -> None:
    assert AUDIT_INTEGRITY_MIGRATION_SOURCE.component == "audit_integrity"
    assert AUDIT_INTEGRITY_MIGRATION_SOURCE.directory == AUDIT_PRIVILEGED_MIGRATIONS_DIR
    assert (
        Path(ps_service.__file__).parent / "audit" / "privileged_migrations"
    ) == AUDIT_PRIVILEGED_MIGRATIONS_DIR
    assert AUDIT_INTEGRITY_MIGRATION_SOURCE not in ORDINARY_STATE_MIGRATION_SOURCES
    assert AUDIT_INTEGRITY_MIGRATION_SOURCE != GRAPH_GATEWAY_MIGRATION_SOURCE


def test_service_startup_never_applies_the_audit_integrity_source() -> None:
    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    applied_by_main = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_state_migrations"
        and any(
            isinstance(inner, ast.Name) and inner.id == "AUDIT_INTEGRITY_MIGRATION_SOURCE"
            for inner in ast.walk(node)
        )
    ]

    assert applied_by_main == []


def test_main_uses_the_shared_sources_and_declares_no_literal_list() -> None:
    tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
    literal_sources = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MigrationSource"
    ]

    assert main_module.ORDINARY_STATE_MIGRATION_SOURCES is (
        state_migrations.ORDINARY_STATE_MIGRATION_SOURCES
    )
    assert main_module.GRAPH_GATEWAY_MIGRATION_SOURCE is (
        state_migrations.GRAPH_GATEWAY_MIGRATION_SOURCE
    )
    assert literal_sources == []
