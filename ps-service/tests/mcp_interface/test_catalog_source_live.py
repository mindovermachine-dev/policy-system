"""`postgres_live` tests for the catalog-source MCP tools against a real Postgres (issue #130).

The full path AC-BI-007/011/012 describe: `set-catalog-source` -> `runtime_config` row and
`audit_events` row committed in one transaction -> readable through `list-audit-events`, plus
CHANGES F4 (the local-test bypass records a sentinel actor). The store-level live tests are in
`tests/curated_source/test_override_live.py`.

Deselected by default -- run with `uv run pytest -m postgres_live` against a reachable
`PS_STATE_POSTGRES_*` instance. The override is one shared row, so every test resets it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING

import pytest
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_authz_gate import (
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- reuse the gate tests' verified-caller helper rather than a third copy
)
from ps_service.audit import MIGRATIONS_DIR as AUDIT_MIGRATIONS_DIR
from ps_service.audit import PsycopgAuditStore
from ps_service.authz import MIGRATIONS_DIR as AUTHZ_MIGRATIONS_DIR
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import ServiceConfig, load_config
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.curated_source.store import reset_override
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.persistence import MigrationSource, apply_pending_migrations, connect_from_config
from ps_service.runtime_config import MIGRATIONS_DIR as RUNTIME_CONFIG_MIGRATIONS_DIR
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.postgres_live

# Mirrors the source list `ps_service.main` passes to the runner at startup.
STATE_MIGRATION_SOURCES = [
    MigrationSource("audit", AUDIT_MIGRATIONS_DIR),
    MigrationSource("authz", AUTHZ_MIGRATIONS_DIR),
    MigrationSource("runtime_config", RUNTIME_CONFIG_MIGRATIONS_DIR),
]

_ISSUER = "https://issuer.example.com/"
_OVERRIDE_URL = "https://example.com/live-override"
_ACTOR = ("live-test-actor", _ISSUER)


@pytest.fixture
def config() -> Iterator[ServiceConfig]:
    """A migrated real config; the shared override row is reset again afterwards."""
    real = load_config()
    assert real.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    with connect_from_config(real) as conn:
        apply_pending_migrations(conn, sources=STATE_MIGRATION_SOURCES)
    yield real
    reset_override(
        PsycopgRuntimeConfigStore(real, audit_store=PsycopgAuditStore(real)), actor=_ACTOR
    )


def _call(tool: str, args: dict[str, object] | None = None) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool(tool, args or {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_set_catalog_source_tool_writes_audit_row_readable_via_list_audit_events(
    config: ServiceConfig,
) -> None:
    configure()
    subject = f"live-admin-{uuid.uuid4().hex[:8]}"
    PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)).grant(
        actor=("system:test-setup", _ISSUER),
        target=(subject, _ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )

    with _verified_actor(sub=subject, iss=_ISSUER):
        set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
        get_result = _call("get-catalog-source")
        audit_result = _call(
            "list-audit-events", {"resource_type": "runtime_config", "actor_subject": subject}
        )
        reset_result = _call("reset-catalog-source")
        after_reset = _call("get-catalog-source")

    assert json.loads(_text(set_result)) == {"url": _OVERRIDE_URL, "source": "override"}
    assert json.loads(_text(get_result)) == {"url": _OVERRIDE_URL, "source": "override"}
    (event,) = json.loads(_text(audit_result))["events"]
    assert (event["action"], event["actor_subject"], event["resource_id"]) == (
        "runtime_config.set",
        subject,
        CATALOG_SOURCE_KEY,
    )
    assert event["outcome"] == "applied"
    assert event["details"] == {"key": CATALOG_SOURCE_KEY, "new_value": _OVERRIDE_URL}
    assert json.loads(_text(reset_result))["source"] == "default"
    assert json.loads(_text(after_reset))["source"] == "default"


def _sentinel_audit_row_count(config: ServiceConfig, action: str) -> int:
    with connect_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_events WHERE resource_id = %(key)s "
            "AND actor_subject = 'system:local-test-bypass' AND action = %(action)s",
            {"key": CATALOG_SOURCE_KEY, "action": action},
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_set_and_reset_under_local_test_bypass_each_write_one_audit_row_for_the_sentinel_actor(
    config: ServiceConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHANGES F4: the bypass has no real actor, and `audit_events` actors are NOT NULL."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    sets_before = _sentinel_audit_row_count(config, "runtime_config.set")
    resets_before = _sentinel_audit_row_count(config, "runtime_config.reset")

    set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
    reset_result = _call("reset-catalog-source")

    assert json.loads(_text(set_result)) == {"url": _OVERRIDE_URL, "source": "override"}
    assert json.loads(_text(reset_result))["source"] == "default"
    assert _sentinel_audit_row_count(config, "runtime_config.set") == sets_before + 1
    assert _sentinel_audit_row_count(config, "runtime_config.reset") == resets_before + 1
