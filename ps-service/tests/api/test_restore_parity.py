"""MCP and REST catalog restores record the same `instrument.restore` rows (issue #195, AC-BI-017).

The MCP tool and `POST /restorations/from-catalog` both delegate to
`run_restoration_from_catalog_source`; this proves the rows depend only on the actor and the
canonical instrument id, not on the transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from api._audit_fakes import InMemoryAuditStore
from api._fakes import compliance_officer_principal, install_compliance_officer_grant
from api.test_restorations_from_catalog import (
    _app_config,  # pyright: ignore[reportPrivateUsage]  -- reuse the REST-side fixtures verbatim
    _client_with_fake,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _FakeCatalogRestoreStage,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _valid_transport,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.models import CatalogRestorationRequest
from ps_service.api.restore_orchestration import run_restoration_from_catalog_source
from ps_service.audit import AuditContext

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _configure_logging(configured_logging: Path) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """`fetch_artifact` always logs: install a real Logging facade."""


def test_mcp_and_rest_from_catalog_restore_record_the_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_compliance_officer_grant(monkeypatch, granted=True)
    principal = compliance_officer_principal()

    # REST transport.
    rest_store = InMemoryAuditStore()
    client = _client_with_fake(
        _valid_transport(), _FakeCatalogRestoreStage(), audit_store=rest_store
    )
    response = client.post("/restorations/from-catalog", json={"instrument_id": "cra-1.0"})
    assert response.status_code == 200

    # The shared orchestration the MCP tool calls, with the MCP-style context.
    mcp_store = InMemoryAuditStore()
    run_restoration_from_catalog_source(
        CatalogRestorationRequest(instrument_id="cra-1.0"),
        config=_app_config(),
        actor="unknown",
        dependencies=_fake_dependencies(_valid_transport(), _FakeCatalogRestoreStage()),
        audit=AuditContext((principal.sub, principal.iss), mcp_store),
    )

    assert rest_store.rows == mcp_store.rows
    assert len(rest_store.rows) == 2
