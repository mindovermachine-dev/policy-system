"""Issue #71 PLAN.md S16 -- the ``falkordb_live``/``llm_live`` export capstone.

Exercises the real ``POST /exports`` route end to end against two real,
already-ingested instruments -- one external, one internal -- proving
Deliverable #5 (issue #71) across the whole HTTP stack rather than only at
the bare ``export_instrument()`` orchestration level.

Per PLAN.md §0 D10, this capstone reuses two already-real, already-permanent
fixture graph pairs instead of seeding new ones:

- **External half:** ``cra_baseline``/``cra_native`` (``RegulatoryInstrument.id
  = "CRA-1.0"``). Rather than assuming these already exist from a prior,
  out-of-order test run (an ordering assumption this plan rejects as
  fragile), this module runs its own bounded, self-contained ``POST
  /ingestions`` catalog call for CRA (CELEX ``32024R2847``) first -- copying
  ``test_live_capstone_external.py``'s own
  ``_LimitedDomainMappingAdapter``/``_EXTRACTION_UNIT_LIMIT`` pattern
  verbatim (bounds LLM cost/time) -- guaranteeing ``CRA-1.0`` is freshly,
  provably ingested before ``POST /exports`` is ever called against it. Only
  the *disposable single-tenant* merge graph this ingestion targets
  (``_DISPOSABLE_GRAPH``) is cleaned up in a ``finally`` -- ``cra_baseline``/
  ``cra_native`` are left permanent, exactly like
  ``test_live_capstone_external.py``'s own ``cra_baseline``/``cra_native``
  side effect.
- **Internal half:** the real, permanent ``engprac_baseline``/
  ``engprac_native`` (``RegulatoryInstrument.id = "ENGPRAC-1.0"``), seeded
  by issue #95's real internal-seed adapter submissions (the incremental
  per-slice submissions of ``internal-sources/engineering-practices/
  engineering-practices-seed.json`` through ``POST /ingestions``) -- no
  setup of its own is needed, exactly like
  ``test_engineering_practices_migration_live.py``.

**Embedding seam:** mirroring ``test_engineering_practices_migration_live.py``'s
own explicit design choice, both ``POST /exports`` calls run through
``app.dependency_overrides[provide_export_dependencies]`` swapped to a bundle
using the **real** ``_default_open_db``/real graph openers/real
``export_instrument`` delegate (``build_default_export_dependencies()``) but
a **fake, deterministic** ``call_embedding`` (``_FakeEmbeddingCaller``,
copied from that module) -- so the capstone needs ``llm_live`` only for the
external half's ingestion pre-step (a real Domain Mapper extraction call),
not for either export.

**Written but not run here:** per BASELINE.md, this sandbox has no live
FalkorDB and the default local gate already excludes
``falkordb_live``/``llm_live`` (``pyproject.toml``). This file is committed,
correctly marked, and collected-then-deselected here -- mirroring
``test_live_capstone_external.py``/``test_live_capstone_internal.py``/
``test_engineering_practices_migration_live.py``'s own existing precedent
exactly (all four are unrun in this sandbox for the identical reason).

The module-level env capture below happens *before* the autouse
``_isolate_logging`` fixture (``tests/conftest.py``) ``delenv``s
``PS_LLMINTERFACE_MODEL``/``_EMBED_MODEL`` for every test -- otherwise the
values are gone by the time the ``capstone`` fixture needs to re-``setenv``
them (the same ordering caveat ``test_live_capstone_external.py`` documents
and fixes the same way).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from fastapi.testclient import TestClient
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.api.dependencies import provide_export_dependencies, provide_pipeline_dependencies
from ps_service.api.export_orchestration import build_default_export_dependencies
from ps_service.api.ingestion_orchestration import build_default_pipeline_dependencies
from ps_service.company_merge.falkordb_client import connect_from_config
from ps_service.config import load_config
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from falkordb import FalkorDB

    from ps_service.api.export_orchestration import ExportDependencies
    from ps_service.api.ingestion_orchestration import PipelineDependencies
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.falkordb_client import GraphHandle as DomainMappingGraphHandle
    from ps_service.domain_mapper.models import ExtractionUnit
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )

# Captured at import time, before the autouse `_isolate_logging` fixture strips them.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")

_EXTRACTION_UNIT_LIMIT = 15
_DISPOSABLE_GRAPH = "policy_system_export_capstone_test"
_INGESTIONS_ENDPOINT = "/ingestions"
_EXPORTS_ENDPOINT = "/exports"

_CRA_CELEX = "32024R2847"
_CRA_INSTRUMENT_ID = "CRA-1.0"
_CRA_REQUEST: dict[str, str] = {"source": "catalog", "celex": _CRA_CELEX}
_CRA_BASELINE_GRAPH = "cra_baseline"
_CRA_NATIVE_GRAPH = "cra_native"

_ENGPRAC_INSTRUMENT_ID = "ENGPRAC-1.0"
_ENGPRAC_BASELINE_GRAPH = "engprac_baseline"
_ENGPRAC_NATIVE_GRAPH = "engprac_native"

_INTERNAL_DEPTH_LABELS = {"Policy", "Standard", "Control"}

pytestmark = [
    pytest.mark.falkordb_live,
    pytest.mark.llm_live,
    pytest.mark.skipif(
        not _CHAT_MODEL or not _EMBED_MODEL,
        reason="requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, AZURE_*)",
    ),
]


# --- external-half ingestion bounding (copied from test_live_capstone_external.py) ---


class _LimitedDomainMappingAdapter:
    """Wraps a real ``DomainMappingAdapter``, capping returned units to the first ``limit``.

    Satisfies ``DomainMappingAdapter`` structurally -- no production change is
    needed for this capstone's bounding requirement. Copied verbatim from
    ``test_live_capstone_external.py`` (itself copied from
    ``tests/domain_mapper/test_live_capstone.py``).
    """

    def __init__(self, inner: DomainMappingAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def read_native_units(self, graph: DomainMappingGraphHandle) -> tuple[ExtractionUnit, ...]:
        """Return the inner adapter's units, truncated to the first ``limit``."""
        return self._inner.read_native_units(graph)[: self._limit]


def _limited_mapping_adapter() -> DomainMappingAdapter:
    """Zero-arg factory for the bounded Cellar/ELI Domain Mapping Adapter."""
    return _LimitedDomainMappingAdapter(CellarEliDomainMappingAdapter(), _EXTRACTION_UNIT_LIMIT)


def _limited_pipeline_dependencies() -> PipelineDependencies:
    """The production ``PipelineDependencies`` with only ``adapters.mapping`` bounded."""
    default = build_default_pipeline_dependencies()
    return replace(default, adapters=replace(default.adapters, mapping=_limited_mapping_adapter))


# --- export embedding seam (D10; copied from test_engineering_practices_migration_live.py) ---


class _FakeEmbeddingCaller:
    """A deterministic embedding stand-in -- no real provider call for either export."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = [float(len(text) % 7), 0.5]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _fake_export_dependencies() -> ExportDependencies:
    """The production ``ExportDependencies`` (real db/graph openers/delegate), fake embedding."""
    default = build_default_export_dependencies()
    return replace(default, call_embedding=_FakeEmbeddingCaller())


# --- small local helpers (module is self-contained, mirroring the external capstone) ---


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- the one field this module reads."""

    @property
    def result_set(self) -> list[object]: ...


def _count(graph: _GraphQueryHandle, query: str) -> int:
    """Run a `RETURN count(...)` query against `graph` and return the scalar result."""
    result = graph.query(query)
    rows = cast("list[list[object]]", cast("_QueryResult", result).result_set)
    return cast("int", rows[0][0])


def _live_counts(graph: _GraphQueryHandle) -> tuple[int, int]:
    """(node count, edge count) read directly off a live graph."""
    return (
        _count(graph, "MATCH (n) RETURN count(n)"),
        _count(graph, "MATCH ()-[r]->() RETURN count(r)"),
    )


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    """Drop ``name`` from FalkorDB if it is currently present (best effort)."""
    if name in db.list_graphs():
        db.select_graph(name).delete()


def _read_lines(log_path: Path) -> list[dict[str, object]]:
    """Read `log_path` (after the emitter is drained) and parse each line as JSON."""
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


@dataclass(frozen=True, slots=True)
class _CapstoneData:
    """Everything this module's assertions read, captured by the one shared run."""

    ingestion_status: int
    cra_export_status: int
    cra_export_body: dict[str, object]
    cra_live_baseline_counts: tuple[int, int]
    cra_live_native_counts: tuple[int, int]
    engprac_export_status: int
    engprac_export_body: dict[str, object]
    engprac_live_baseline_counts: tuple[int, int]
    engprac_live_native_counts: tuple[int, int]
    log_lines: list[dict[str, object]]


@pytest.fixture(scope="module")
def capstone(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_CapstoneData]:
    """Ingest CRA (bounded), then export both CRA and ENGPRAC over the real HTTP route.

    The Domain Mapper stages and the export route both emit through the
    process-wide default emitter (neither passes an explicit one), so a real
    `configure()`d facade is installed for the run, mirroring
    `test_live_capstone_external.py`'s own fixture exactly.
    `reset_for_tests()` leaves the module-global `_atexit_registered` guard
    set; it is saved and restored here so this live-only module can never
    poison `tests/logging`'s once-only `atexit` assertion if the two are
    ever collected together.

    Yields:
        The captured :class:`_CapstoneData`.
    """
    assert _CHAT_MODEL is not None  # narrowed by the module skipif
    assert _EMBED_MODEL is not None

    monkeypatch = pytest.MonkeyPatch()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]

    config = load_config()
    db = connect_from_config(config)

    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", _CHAT_MODEL)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", _EMBED_MODEL)
    if not os.environ.get("PS_COMPANYMERGE_SIMILARITY_THRESHOLD"):
        monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _DISPOSABLE_GRAPH)

    _delete_graph_if_exists(db, _DISPOSABLE_GRAPH)
    log_path = tmp_path_factory.mktemp("export_capstone") / "capstone.jsonl"
    facade.configure(log_path=log_path)

    app = create_app(load_config())
    app.dependency_overrides[provide_pipeline_dependencies] = _limited_pipeline_dependencies
    app.dependency_overrides[provide_export_dependencies] = _fake_export_dependencies

    try:
        client = TestClient(app, raise_server_exceptions=False)

        ingestion_response = client.post(_INGESTIONS_ENDPOINT, json=_CRA_REQUEST)
        if ingestion_response.status_code != 200:
            message = (
                f"CRA ingestion POST returned {ingestion_response.status_code}: "
                f"{ingestion_response.text}"
            )
            pytest.fail(message)

        cra_response = client.post(_EXPORTS_ENDPOINT, json={"instrument_id": _CRA_INSTRUMENT_ID})
        engprac_response = client.post(
            _EXPORTS_ENDPOINT, json={"instrument_id": _ENGPRAC_INSTRUMENT_ID}
        )

        cra_baseline_handle = graph_query_handle(db, _CRA_BASELINE_GRAPH)
        cra_native_handle = graph_query_handle(db, _CRA_NATIVE_GRAPH)
        engprac_baseline_handle = graph_query_handle(db, _ENGPRAC_BASELINE_GRAPH)
        engprac_native_handle = graph_query_handle(db, _ENGPRAC_NATIVE_GRAPH)

        facade.reset_for_tests()  # drain + join the writer thread so the file is complete
        log_lines = _read_lines(log_path)

        yield _CapstoneData(
            ingestion_status=ingestion_response.status_code,
            cra_export_status=cra_response.status_code,
            cra_export_body=cast("dict[str, object]", cra_response.json()),
            cra_live_baseline_counts=_live_counts(cra_baseline_handle),
            cra_live_native_counts=_live_counts(cra_native_handle),
            engprac_export_status=engprac_response.status_code,
            engprac_export_body=cast("dict[str, object]", engprac_response.json()),
            engprac_live_baseline_counts=_live_counts(engprac_baseline_handle),
            engprac_live_native_counts=_live_counts(engprac_native_handle),
            log_lines=log_lines,
        )
    finally:
        monkeypatch.undo()
        with contextlib.suppress(Exception):
            db.select_graph(_DISPOSABLE_GRAPH).delete()
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def _parsed_blob_counts(body: dict[str, object], *, field: str) -> tuple[int, int]:
    """Decode + parse a base64 `*_blob_base64` field, returning (node count, edge count)."""
    blob = base64.b64decode(cast("str", body[field]))
    parsed = parse_serialized_graph_json(blob)
    return (len(parsed.nodes), len(parsed.edges))


def test_cra_export_returns_200_with_external_manifest(capstone: _CapstoneData) -> None:
    """AC-BI-003/Deliverable #5: the external half's manifest is real and server-derived."""
    assert capstone.ingestion_status == 200
    assert capstone.cra_export_status == 200
    manifest = cast("dict[str, object]", capstone.cra_export_body["manifest"])
    assert manifest["source_type"] == "external"
    assert manifest["celex"] == _CRA_CELEX
    assert manifest["jurisdiction"] == "EU"


def test_cra_export_blobs_are_lossless_against_the_live_graphs(capstone: _CapstoneData) -> None:
    """The scratch-directory round trip (D1) drops/duplicates nothing for the external half."""
    baseline_counts = _parsed_blob_counts(capstone.cra_export_body, field="baseline_blob_base64")
    native_counts = _parsed_blob_counts(capstone.cra_export_body, field="native_blob_base64")
    assert baseline_counts == capstone.cra_live_baseline_counts
    assert native_counts == capstone.cra_live_native_counts


def test_engprac_export_returns_200_with_internal_manifest(capstone: _CapstoneData) -> None:
    """AC-BI-003/Deliverable #5: the internal half's manifest is real and server-derived."""
    assert capstone.engprac_export_status == 200
    manifest = cast("dict[str, object]", capstone.engprac_export_body["manifest"])
    assert manifest["source_type"] == "internal"
    assert manifest["celex"] is None
    assert manifest["jurisdiction"] is None


def test_engprac_export_blobs_are_lossless_against_the_live_graphs(
    capstone: _CapstoneData,
) -> None:
    """The scratch-directory round trip (D1) drops/duplicates nothing for the internal half."""
    baseline_counts = _parsed_blob_counts(
        capstone.engprac_export_body, field="baseline_blob_base64"
    )
    native_counts = _parsed_blob_counts(capstone.engprac_export_body, field="native_blob_base64")
    assert baseline_counts == capstone.engprac_live_baseline_counts
    assert native_counts == capstone.engprac_live_native_counts


def test_engprac_baseline_document_includes_policy_standard_control_labels(
    capstone: _CapstoneData,
) -> None:
    """D15 depth check, now proven reachable through the HTTP route, not just the bare delegate."""
    blob = base64.b64decode(cast("str", capstone.engprac_export_body["baseline_blob_base64"]))
    document = cast("dict[str, object]", json.loads(blob))
    labels_present = {
        cast("str", node["label"]) for node in cast("list[dict[str, object]]", document["nodes"])
    }
    assert labels_present >= _INTERNAL_DEPTH_LABELS


def test_audit_log_records_started_and_succeeded_for_both_exports(
    capstone: _CapstoneData,
) -> None:
    """AC-BI-013: both exports produce started/succeeded entries with correct id/schema_version."""
    entries = [
        row
        for row in capstone.log_lines
        if row.get("component") == "export" and row.get("action") == "export_instrument"
    ]
    for instrument_id in (_CRA_INSTRUMENT_ID, _ENGPRAC_INSTRUMENT_ID):
        instrument_entries = [row for row in entries if row.get("entity_id") == instrument_id]
        outcomes = {row.get("outcome") for row in instrument_entries}
        assert {"started", "succeeded"} <= outcomes, instrument_id
        assert all(
            row.get("schema_version") == DOMAIN_SCHEMA_VERSION for row in instrument_entries
        ), instrument_id
