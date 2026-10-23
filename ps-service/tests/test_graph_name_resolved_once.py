"""Source guard: the single-tenant graph name is read from the environment only by `config.py`.

The `PS_FALKORDB_GRAPH` variable is resolved once by `load_config()` into
`ServiceConfig.falkordb_graph`. Any other module under `ps_service` that reads the variable
from the process environment is a second source of the name.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_change_check_dependencies, build_fake_pipeline_dependencies
from api._gateway_fakes import process_gateway_installed
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)
from graph_gateway._fakes import close_all_rigs
from policy_lifecycle._gateway_fakes import (
    LifecycleRig,
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedStandard,
)

import ps_service
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.ingestion_orchestration import (
    _open_single_tenant_graph as open_ingestion_single_tenant_graph,  # pyright: ignore[reportPrivateUsage]  -- the opener the pipeline wires by default
)
from ps_service.api.ingestion_orchestration import run_catalog_ingestion_pipeline
from ps_service.api.models import ExportRequest, RestorationRequest
from ps_service.api.near_miss_review_orchestration import (
    build_default_near_miss_review_graph_opener,
)
from ps_service.api.restore_orchestration import RestoreDependencies, run_restoration
from ps_service.audit import AuditContext
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.config import ServiceConfig, load_config
from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_graph_opener
from ps_service.mcp_interface.tool_core import resolve_graph
from ps_service.policy_lifecycle.service import (
    create_policy_draft,
    get_policy,
    propose_policy,
    update_policy_draft,
)
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from collections.abc import Callable

    from api._fakes import MakeEmitter

_SRC = Path(ps_service.__file__).parent
_VARIABLE = "PS_FALKORDB_GRAPH"
_ENVIRONMENT_MARKER = "environ"
_CONFIG_MODULE = "config.py"


def _environment_readers(source_root: Path) -> set[str]:
    """Return modules (relative to `source_root`) with a line naming the variable and `environ`."""
    readers: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if _VARIABLE in line and _ENVIRONMENT_MARKER in line:
                readers.add(relative)
                break
    return readers


def test_only_the_configuration_module_reads_the_graph_name_from_the_environment() -> None:
    """Only `config.py` reads the variable from the environment."""
    assert _environment_readers(_SRC) == {_CONFIG_MODULE}


def test_the_checker_flags_a_planted_environment_read(tmp_path: Path) -> None:
    """The scan reports a module that reads the variable and ignores one that does not."""
    (tmp_path / "reader.py").write_text(
        f'import os\nname = os.environ.get("{_VARIABLE}", "x")\n', encoding="utf-8"
    )
    (tmp_path / "plain.py").write_text(f'NAME = "{_VARIABLE}"\n', encoding="utf-8")

    assert _environment_readers(tmp_path) == {"reader.py"}


def test_the_change_monitor_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter
) -> None:
    """The sweep hands `trigger_reingestion` the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = load_config()
    monkeypatch.setenv(_VARIABLE, "second_name")
    emitter, _ = make_emitter()
    fake = build_fake_change_check_dependencies(
        tracked=(
            TrackedInstrumentNode(
                regulatory_instrument_id="CRA-1",
                celex="32024R2847",
                instrument_type="regulation",
                effective_date="2024-01-01",
            ),
        ),
        poll_report=PollReport(
            findings=(
                AmendmentFinding(
                    regulatory_instrument_id="CRA-1",
                    instrument_type="regulation",
                    baseline_reference="2024-01-01",
                    detected_consolidated_celex="32024R2847C01",
                    detected_consolidation_date=date(2025, 1, 1),
                    reason="newer_consolidation",
                ),
            ),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={
            "32024R2847": CatalogEntry(
                celex="32024R2847", title="Cyber Resilience Act", short_name="CRA", version="1.0"
            )
        },
        reingestion_result=ReingestionOutcome(
            prior_regulatory_instrument_id="CRA-0.9",
            new_regulatory_instrument_id="CRA-32024R2847C01",
            run_id=None,
            outcome="superseded",
        ),
    )

    with process_gateway_installed():
        run_change_check_sweep(
            config=config,
            run_id="sweep-run",
            dependencies=fake.dependencies,
            audit=AuditContext(("caller", "https://issuer.example.com/"), InMemoryAuditStore()),
            emitter=emitter,
        )

    (call,) = fake.trigger_reingestion_calls
    assert call.single_tenant_graph_name == "first_name"


_OWNER = ("alice", "https://issuer.example")
_POLICY = "pol_x"


def _seed(rig: LifecycleRig, graph_name: str, *, version: str | None = "1") -> PolicyReadGraph:
    graph = rig.graphs.open(graph_name)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        title="Title",
        version=version,
        owner=_OWNER,
        standards=(SeedStandard("std_1", "S1"),),
        legacy_edges=False,
    )
    return PolicyReadGraph(graph)


def _create(rig: LifecycleRig, graph_name: str) -> None:
    create_policy_draft(
        actor=_OWNER,
        title="Data Protection Policy",
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _propose(rig: LifecycleRig, graph_name: str) -> None:
    propose_policy(
        actor=_OWNER,
        policy_id=_POLICY,
        graph=_seed(rig, graph_name),
        audit_store=rig.audit,
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _edit(rig: LifecycleRig, graph_name: str) -> None:
    update_policy_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        fields={"description": "d"},
        graph=_seed(rig, graph_name),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _read_with_backfill(rig: LifecycleRig, graph_name: str) -> None:
    get_policy(
        policy_id=_POLICY,
        graph=_seed(rig, graph_name, version=None),
        submitter_source=lambda: rig.gateway,
        graph_name=graph_name,
    )


@pytest.mark.parametrize("drive", [_create, _propose, _edit, _read_with_backfill])
def test_policy_lifecycle_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, drive: Callable[[LifecycleRig, str], None]
) -> None:
    """Each lifecycle write lands on the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = load_config()
    monkeypatch.setenv(_VARIABLE, "second_name")
    rig = LifecycleRig()
    try:
        drive(rig, config.falkordb_graph)

        (group,) = rig.store.groups
        assert group.graph == "first_name"
    finally:
        close_all_rigs()


class _RecordingDatabase:
    """A database whose `select_graph` records the name it was asked for."""

    def __init__(self) -> None:
        self.selected: list[str] = []

    def connect(self, _config: ServiceConfig) -> _RecordingDatabase:
        return self

    def select_graph(self, name: str) -> object:
        self.selected.append(name)
        return object()


def _open_graph_cleanup(config: ServiceConfig) -> None:
    build_default_graph_cleanup_graph_opener()(config)


def _open_near_miss_review(config: ServiceConfig) -> None:
    build_default_near_miss_review_graph_opener()(config)


def _open_ingestion(config: ServiceConfig) -> None:
    open_ingestion_single_tenant_graph(config)


@pytest.mark.parametrize(
    "open_graph", [_open_graph_cleanup, _open_near_miss_review, _open_ingestion]
)
def test_the_read_openers_keep_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, open_graph: Callable[[ServiceConfig], None]
) -> None:
    """Each default opener selects the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = load_config()
    monkeypatch.setenv(_VARIABLE, "second_name")
    database = _RecordingDatabase()
    monkeypatch.setattr(
        "ps_service.company_merge.falkordb_client.connect_from_config",
        database.connect,
    )

    open_graph(config)

    assert database.selected == ["first_name"]


def test_the_catalog_merge_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter
) -> None:
    """The ingestion merge stage receives the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = dataclasses.replace(
        load_config(),
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=0.5,
        company_merge_role_review_threshold=0.5,
    )
    monkeypatch.setenv(_VARIABLE, "second_name")
    emitter, _ = make_emitter()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")

    with process_gateway_installed():
        run_catalog_ingestion_pipeline(
            CatalogEntry(celex="32024R2847", title="CRA", short_name="CRA", version="1.0"),
            config=config,
            run_id="run-resolved-once",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            emitter=emitter,
        )

    merge_kwargs = next(call.kwargs for call in fake.recorder.calls if call.stage == "merge")
    assert merge_kwargs["single_tenant_graph_name"] == "first_name"


class _ExportDatabase:
    """A database listing one baseline graph; the single-tenant opener below ends the run."""

    def list_graphs(self) -> list[str]:
        return ["cra_baseline"]


class _ExportBaselineGraph:
    """Answers the one query the export issues to learn the instrument's properties."""

    def query(self, _query: str, params: dict[str, object] | None = None) -> object:
        del params
        return type(
            "_Result",
            (),
            {
                "result_set": [
                    [{"id": "CRA-1.0", "source_type": "external", "version": "1.0", "title": "CRA"}]
                ]
            },
        )()


class _GateReachedError(Exception):
    """Raised by the opener once the near-miss gate asked for its graph."""


def test_the_export_gate_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter
) -> None:
    """The export near-miss gate opens the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = dataclasses.replace(load_config(), llm_interface_embed_model="azure/embed")
    monkeypatch.setenv(_VARIABLE, "second_name")
    opened: list[str] = []

    def open_single_tenant(_db: object, graph_name: str) -> object:
        opened.append(graph_name)
        raise _GateReachedError

    dependencies = ExportDependencies(
        open_db=lambda _config: cast("Any", _ExportDatabase()),
        open_baseline_graph=lambda _db, _short: cast("Any", _ExportBaselineGraph()),
        open_native_graph=lambda _db, _short: object(),  # pyright: ignore[reportArgumentType]
        export=cast("Any", None),
        call_embedding=None,
        graph_write_access=graph_write_access,
        open_single_tenant_graph=cast("Any", open_single_tenant),
    )

    with process_gateway_installed(), pytest.raises(_GateReachedError):
        run_export(
            ExportRequest.model_validate({"instrument_id": "CRA-1.0"}),
            config=config,
            actor="x",
            dependencies=dependencies,
            emitter=make_emitter()[0],
        )

    assert opened == ["first_name"]


_RESTORE_MANIFEST: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1",
    "exported_at": "2026-01-01T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}


def test_the_restore_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore routine receives the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = dataclasses.replace(
        load_config(),
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=0.5,
        company_merge_role_review_threshold=0.5,
    )
    monkeypatch.setenv(_VARIABLE, "second_name")
    names: list[str] = []

    def restore(_artifact: object, *, single_tenant_graph_name: str, **_rest: object) -> object:
        names.append(single_tenant_graph_name)
        return RestoreOutcome(
            instrument_id="CRA-1.0",
            stages=("verified", "staged", "merged_and_finalized"),
            near_miss_reviews_created=0,
        )

    dependencies = RestoreDependencies(
        open_db=lambda _config: cast("Any", object()),
        restore=cast("Any", restore),
        graph_write_access=graph_write_access,
    )

    run_restoration(
        RestorationRequest.model_validate(
            {
                "instrument_id": "CRA-1.0",
                "manifest": _RESTORE_MANIFEST,
                "baseline_blob_base64": base64.b64encode(b"{}").decode("ascii"),
                "native_blob_base64": base64.b64encode(b"{}").decode("ascii"),
            }
        ),
        config=config,
        actor="x",
        dependencies=dependencies,
        audit=AuditContext(("caller", "https://issuer.example.com/"), InMemoryAuditStore()),
    )

    assert names == ["first_name"]


_LIVE_MERGE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "tools" / "company-merge" / "run_live_merge.py"
)


class _ProviderReachedError(Exception):
    """Raised by the graph provider once the approval gate let the run through."""


def test_the_live_merge_approval_gate_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The approval is checked against the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = dataclasses.replace(
        load_config(),
        llm_interface_embed_model="azure/embed",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=0.5,
        company_merge_role_review_threshold=0.5,
    )
    monkeypatch.setenv(_VARIABLE, "second_name")
    approval = tmp_path / "approval.md"
    approval.write_text(
        "approved_by: operator\n"
        "approved_at: 2026-09-13T10:00:00Z\n"
        "scope: CRA-1.0,GDPR-1.0,NIS2-1.0\n"
        "target_graph: first_name\n"
        "precondition_check_result: cra_baseline: 0 violations - GO\n"
        "confirmation_phrase: I APPROVE THE LIVE MERGE INTO policy_system\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("_live_merge_guard", _LIVE_MERGE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_live_merge_guard", module)
    spec.loader.exec_module(module)
    provider_calls: list[str] = []

    def provider(graph_name: str) -> object:
        provider_calls.append(graph_name)
        raise _ProviderReachedError

    exit_code = module.main(
        ["--approval-file", str(approval), "--run-kind", "initial"],
        graph_provider=provider,
        load_service_config=lambda: config,
    )

    assert exit_code == 1
    assert provider_calls != []


def test_the_mcp_graph_keeps_the_graph_name_resolved_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_graph` selects the name `load_config()` resolved, not a later one."""
    monkeypatch.setenv(_VARIABLE, "first_name")
    config = load_config()
    monkeypatch.setenv(_VARIABLE, "second_name")
    database = _RecordingDatabase()
    monkeypatch.setattr("ps_service.mcp_interface.tool_core.connect_from_config", database.connect)

    resolve_graph(config)

    assert database.selected == ["first_name"]
