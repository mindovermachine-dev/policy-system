"""GH #104 AC-BI-004 -- restore the shipped ENGPRAC-1.0 artifact over ``POST /restorations``.

``falkordb_live`` against the dev-container FalkorDB only (``PS_FALKORDB_HOST``/``_PORT``).
The real ``RestoreDependencies`` bundle is used with exactly one seam swapped -- the single-tenant
graph name (precedent: ``test_live_capstone_export.py``'s embedding seam) -- and the manifest's
``short_name`` is tokened, so every key the run writes is unique to this test and the permanent
``engprac_baseline``/``engprac_native``/``policy_system`` graphs are never touched.

Checksums cover the two blobs only, so the shipped bytes and digests are restored unmodified;
only the target graph names (derived from ``short_name.lower()``) change. Cleanup deletes the
three graphs this test names plus any FalkorDB-owned ``telemetry{...}`` key created for them,
and the module's last assertion proves no key carrying the run token remains.

Never assert ``PracticeArea`` in the single-tenant graph: Company Merge's ``graph_reader`` does
not read the classification layer, so the merged leg carries none (PLAN A6 -- a follow-up, not
a defect of this fix).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from ps_service.api.dependencies import provide_restore_dependencies
from ps_service.api.restore_orchestration import build_default_restore_dependencies
from ps_service.company_merge.falkordb_client import connect_from_config
from ps_service.config import load_config
from ps_service.export import catalog_writer
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.main import create_app

if TYPE_CHECKING:
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.api.restore_orchestration import RestoreDependencies
    from ps_service.config import ServiceConfig

pytestmark = pytest.mark.falkordb_live

_INSTRUMENT_DIR = Path(__file__).resolve().parents[3] / "curated-content" / "ENGPRAC-1.0"
_INSTRUMENT_ID = "ENGPRAC-1.0"
_RESTORATIONS_ENDPOINT = "/restorations"
_SIMILARITY_THRESHOLD = 0.9
_EXPECTED_STAGES = ["verified", "staged", "merged_and_finalized"]


def _restoration_body(short_name: str) -> tuple[dict[str, object], bytes]:
    """The shipped ENGPRAC-1.0 artifact as a request body, plus the raw native blob."""
    manifest = replace(catalog_writer.read_manifest(_INSTRUMENT_DIR), short_name=short_name)
    baseline_blob = (_INSTRUMENT_DIR / "baseline.json").read_bytes()
    native_blob = (_INSTRUMENT_DIR / "native.json").read_bytes()
    body: dict[str, object] = {
        "instrument_id": manifest.instrument_id,
        "manifest": asdict(manifest),
        "baseline_blob_base64": base64.b64encode(baseline_blob).decode("ascii"),
        "native_blob_base64": base64.b64encode(native_blob).decode("ascii"),
    }
    return body, native_blob


def _count(db: FalkorDB, graph_name: str, cypher: str) -> int:
    """Run a single-scalar ``RETURN count(...)`` query against ``graph_name``."""
    rows = cast("list[list[object]]", db.select_graph(graph_name).query(cypher).result_set)
    return cast("int", rows[0][0])


def _keys(db: FalkorDB, pattern: str) -> list[str]:
    """Every key matching ``pattern`` (FalkorDB's client decodes responses, so keys are ``str``)."""
    return cast(
        "list[str]",
        db.connection.keys(pattern),  # pyright: ignore[reportUnknownMemberType] -- redis-py: `.keys()`'s own stub signature carries an Unknown `**kwargs`
    )


def _graph_keys(db: FalkorDB, pattern: str) -> list[str]:
    """``_keys`` minus FalkorDB's own ``telemetry{...}`` metadata keys."""
    return [key for key in _keys(db, pattern) if not key.startswith("telemetry{")]


def _delete_everything_created_for(db: FalkorDB, token: str, graph_names: tuple[str, ...]) -> None:
    """Delete this test's graphs, then any FalkorDB-owned ``telemetry{...}`` key naming them."""
    db.connection.delete(*graph_names)
    telemetry_keys = _keys(db, f"telemetry{{*{token}*}}")
    if telemetry_keys:
        db.connection.delete(*telemetry_keys)


@pytest.mark.usefixtures("configured_logging")
def test_shipped_engprac_artifact_restores_with_every_stage_succeeded() -> None:
    token = uuid.uuid4().hex[:12]
    short_name = f"ENGPRAC_T{token}"
    native_target = f"{short_name.lower()}_native"
    baseline_target = f"{short_name.lower()}_baseline"
    single_tenant_graph_name = f"__gh104_engprac_single_tenant_{token}__"
    config = replace(load_config(), company_merge_similarity_threshold=_SIMILARITY_THRESHOLD)
    db = connect_from_config(config)

    def _tokened_single_tenant_graph_name(_config: ServiceConfig) -> str:
        return single_tenant_graph_name

    def _tokened_dependencies() -> RestoreDependencies:
        return replace(
            build_default_restore_dependencies(),
            single_tenant_graph_name=_tokened_single_tenant_graph_name,
        )

    app = create_app(config)
    app.dependency_overrides[provide_restore_dependencies] = _tokened_dependencies
    client = TestClient(app, raise_server_exceptions=False)
    body, native_blob = _restoration_body(short_name)

    try:
        response = client.post(_RESTORATIONS_ENDPOINT, json=body)

        assert response.status_code == 200, response.text
        payload = cast("dict[str, object]", response.json())
        stages = cast("list[dict[str, str]]", payload["stages"])
        assert payload["instrument_id"] == _INSTRUMENT_ID
        assert [stage["stage"] for stage in stages] == _EXPECTED_STAGES
        assert all(stage["status"] == "succeeded" for stage in stages)

        assert db.connection.exists(native_target) == 1
        assert db.connection.exists(baseline_target) == 1
        expected_native_nodes = len(parse_serialized_graph_json(native_blob).nodes)
        assert _count(db, native_target, "MATCH (n) RETURN count(n)") == expected_native_nodes
        # The widened list let the classification layer through (not a shortcut).
        assert _count(db, baseline_target, "MATCH (n:PracticeArea) RETURN count(n)") > 0
        # The internal Policy merge pass ran (A6: never assert PracticeArea here).
        assert _count(db, single_tenant_graph_name, "MATCH (n:Policy) RETURN count(n)") > 0
        # No staged key leaked past the finalize step.
        assert _graph_keys(db, f"*{token}*__restoring__*") == []
    finally:
        _delete_everything_created_for(
            db, token, (native_target, baseline_target, single_tenant_graph_name)
        )
    assert _keys(db, f"*{token}*") == []
