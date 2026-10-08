"""MCP and REST sync ingests record equivalent `ingestion_run` rows (issue #195, AC-BI-017).

`POST /ingestions` and the `ingest_regulation` MCP tool both call
`run_audited_catalog_ingestion`; this proves the rows depend only on the actor and the run, not on
the transport.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    build_fake_pipeline_dependencies,
    compliance_officer_principal,
    install_compliance_officer_grant,
)
from api.test_ingestions_catalog import (
    _VALID_CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the REST-side fixtures verbatim
    _VALID_SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _app_config,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _client_with_fake,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.ingestion_orchestration import run_audited_catalog_ingestion
from ps_service.audit import AuditContext

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _configure_logging(configured_logging: Path) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """The pipeline always logs: install a real Logging facade."""


def test_mcp_and_rest_sync_ingest_record_equivalent_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    install_compliance_officer_grant(monkeypatch, granted=True)
    principal = compliance_officer_principal()

    # REST transport.
    rest_store = InMemoryAuditStore()
    client = _client_with_fake(
        build_fake_pipeline_dependencies(rid="CRA-1.0").dependencies, audit_store=rest_store
    )
    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )
    assert response.status_code == 200
    rest_run_id = response.json()["run_id"]

    # The shared orchestration the MCP tool calls, with the MCP-style context.
    mcp_store = InMemoryAuditStore()
    run_audited_catalog_ingestion(
        _VALID_CELEX,
        _VALID_SHORT_NAME,
        config=_app_config(),
        run_id="mcp-run",
        caller="unknown",
        dependencies=build_fake_pipeline_dependencies(rid="CRA-1.0").dependencies,
        audit=AuditContext((principal.sub, principal.iss), mcp_store),
    )

    assert len(rest_store.rows) == len(mcp_store.rows) == 2
    # Identical apart from the run id, which is the resource id of every row.
    assert {r.resource_id for r in rest_store.rows} == {rest_run_id}
    assert [dataclasses.replace(r, resource_id="") for r in rest_store.rows] == [
        dataclasses.replace(r, resource_id="") for r in mcp_store.rows
    ]
