"""Tests for the registered `restore_instrument` MCP tool (issue #127, Group 2).

Slice 2.1 covers the happy path: `restore_instrument(instrument_id=...)`
(D-INSTRUMENT-ID-STRICTNESS: `instrument_id` validated at the MCP schema
layer against `_RESTORE_INSTRUMENT_ID_PATTERN`, at least as strict as
ps-cli's own `_instrument_id_type` callback) delegates in-process to
`run_restoration_from_catalog_source` (D-RESTORE-DELEGATE -- the exact same
function `POST /restorations/from-catalog`'s own route calls), via the
shared `_run_mcp_action` audit wrapper (D-AUDIT-WRAPPER), returning the
returned `RestorationAcceptedResponse.model_dump()` verbatim -- the same
`instrument_id`/`stages` shape ps-cli's own `restore instrument` used to
print.

Slice 2.2 (added below in a follow-up edit) wires the remaining
D-SANITIZE-RESTORE error-taxonomy rows and the residual unexpected-exception
safety net.

Slice 2.3 (added below in a follow-up edit) proves principal resolution
(both the `mcp_interface` audit log and the delegate's own `actor` kwarg)
under a real verified bearer token.

issue #163 Slice E: `build_default_restore_from_catalog_dependencies`'s DI
seam is narrowed so only its `fetch_artifact`/`resolve_effective_source`/
`open_db` boundary bundle (`CatalogRestoreInfra`) is substitutable
(`restore_orchestration.py`'s own module docstring) -- `restore` is always
the real, shipped `ps_service.restore.restore_instrument.restore_instrument`
now. This file therefore no longer fakes `restore` at all: every scenario
that can be reached without a real GRAPH.COPY/WATCH-guarded FalkorDB round
trip (checksum/schema_version/content-validation rejection, every
pre-restore failure mode, and the never-reached-the-delegate cases) drives
the REAL `restore_instrument` against a real, correctly-checksummed
artifact via `_manifest`/`_transport_with`. The handful of scenarios that
need the whole staged-write sequence to *succeed* (GRAPH.COPY/WATCH-guarded
RENAME finalize) get there by substituting only the `open_db` boundary
(`_use_fake_restore_infra`'s own default, `_default_open_db_stub`) with a
real, stateful, in-memory FalkorDB-shaped test-data builder
(`_FakeStagingFalkorDB`, below) -- duplicated verbatim from `tests/restore/
test_restore_instrument_audit_log.py`'s own identically-named fixture
(Slice I, IMPL_SLICE_8.md), not imported (see that fixture's own leading
comment for why), so `stage_graph`/`stage_and_finalize_policy_system_leg`/
`raw_connection` all run FOR REAL against it, exactly as that file's own
tests already prove. No `# detroit-exception:` escape hatch is needed here
any more (issue #163 Slice 19: the prior raw no-op stubs for those two
functions, and the docstring justifying them, are gone).

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_get_catalog_listing_tool.py`'s/`test_restorations_from_catalog.py`'s
own convention. `FakeCuratedArtifactTransport`/`FakeFailingCuratedSourceTransport`
(`tests/api/_fakes.py`) are the existing fixtures the REST-side
`POST /restorations/from-catalog` test suite already uses -- reused here
unchanged, not reinvented, per PLAN.md's own instruction. `_fetch_artifact_through`
mirrors `test_restorations_from_catalog.py`'s own identically-shaped helper
(that file's fixture is local to it, not exported, so it is mirrored here
rather than imported).

`pytest-asyncio` is not installed; this file drives the tool with a bare
`asyncio.run(server.call_tool(...))`, exactly like
`test_get_catalog_listing_tool.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from api._fakes import FakeCuratedArtifactTransport, FakeFailingCuratedSourceTransport
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- issue #145: same cross-package import `test_catalog_source_authz_gate.py` already establishes, reused here so this file's own real-token success test can grant the caller `ComplianceOfficer` on the new gate
    FakeAccessRoleStore,
)
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from ps_service.api.restore_orchestration import CatalogRestoreInfra
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.authz.models import AccessRole
from ps_service.company_merge.falkordb_client import single_tenant_graph_name
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.curated_source.artifact_client import fetch_artifact
from ps_service.curated_source.resolve import EffectiveCatalogSource
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)
from ps_service.runtime_config import RuntimeConfigUnavailableError
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    import urllib.request
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.config import ServiceConfig
    from ps_service.curated_source.artifact_client import FetchArtifactCall, FetchedArtifact
    from ps_service.curated_source.http_fetch import CuratedSourceTransport
    from ps_test_support.mock_oidc_provider import MockOidcProvider

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})

_INSTRUMENT_ID = "CRA-1.0"
_EMPTY_GRAPH_BYTES = to_json_bytes(SerializedGraph(nodes=(), edges=()))


def _manifest(**overrides: object) -> dict[str, object]:
    """A valid `InstrumentManifest` payload, real checksums included.

    Every field is real/self-consistent by default (`baseline_sha256`/
    `native_sha256` computed from `_EMPTY_GRAPH_BYTES`, `schema_version` the
    real `DOMAIN_SCHEMA_VERSION`) so the real `restore_instrument`'s D9/D10
    verification passes unless a caller deliberately overrides a field to
    break it.
    """
    manifest: dict[str, object] = {
        "instrument_id": _INSTRUMENT_ID,
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "short_name": "CRA",
        "version": "1.0",
        "source_type": "external",
        "jurisdiction": "EU",
        "schema_version": DOMAIN_SCHEMA_VERSION,
        "exported_at": "2026-01-01T00:00:00Z",
        "baseline_sha256": checksum_bytes(_EMPTY_GRAPH_BYTES),
        "native_sha256": checksum_bytes(_EMPTY_GRAPH_BYTES),
    }
    manifest.update(overrides)
    return manifest


def _transport_with(
    *, native_blob: bytes = _EMPTY_GRAPH_BYTES, **manifest_overrides: object
) -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "manifest.json": json.dumps(_manifest(**manifest_overrides)).encode("utf-8"),
            "baseline.json": _EMPTY_GRAPH_BYTES,
            "native.json": native_blob,
        }
    )


def _valid_transport() -> FakeCuratedArtifactTransport:
    return _transport_with()


def _content_violating_transport() -> FakeCuratedArtifactTransport:
    """A transport whose native leg carries a label outside `NATIVE_ALLOWED_LABELS`.

    Its checksum is computed from the actual (violating) bytes, so D9 passes
    and the real `_validate_content` is the thing that rejects it (GH #104)
    -- zero FalkorDB calls either way, exactly like a checksum/schema_version
    rejection.
    """
    bad_native_bytes = to_json_bytes(
        SerializedGraph(
            nodes=(SerializedNode(label="EvilLabel", properties={"id": "x"}),), edges=()
        )
    )
    return _transport_with(
        native_blob=bad_native_bytes, native_sha256=checksum_bytes(bad_native_bytes)
    )


def _fetch_artifact_through(transport: CuratedSourceTransport) -> FetchArtifactCall:
    def _call(base_url: str, instrument_id: str) -> FetchedArtifact:
        return fetch_artifact(base_url, instrument_id, transport=transport)

    return _call


def _resolve_effective_source_stub(config: ServiceConfig) -> EffectiveCatalogSource:
    return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)


# --------------------------------------------------------------------------
# A real, stateful in-memory FalkorDB-shaped test-data builder (issue #163 Slice 19).
#
# Duplicated from `tests/restore/test_restore_instrument_audit_log.py`'s own
# `_FakeStagingFalkorDB`/`_FakeStagedGraph`/`_FakeSingleTenantGraph`/
# `_FakeWatchablePipeline`/`_FakeRawConnection` (Slice I, IMPL_SLICE_8.md),
# verbatim -- NOT imported, per this test suite's own established
# per-file-duplication convention (that file's own `_FakeSingleTenantGraph`
# docstring: "duplicated per this test suite's existing per-component
# convention" from `test_restore_instrument_classification_passthrough.py`).
# A cross-package import (`mcp_interface` -> `restore`) was tried first and
# rejected: pytest's importlib import mode registers each package in
# `sys.modules` lazily, the first time IT collects a file belonging to that
# package; since `mcp_interface` sorts alphabetically before `restore`,
# `sys.modules["restore"]` is not yet populated when this file is collected
# in a fresh worker process (confirmed empirically: reproduces under the
# exact `uv run pytest -q -n auto --dist=loadscope` invocation this repo's
# own CI/exit-criteria command uses, not just a narrow-subset artifact) --
# duplication sidesteps that hazard entirely.
#
# Previously (before issue #163 Slice 19), this file's own
# `_use_real_restore_with_staging_faked` monkeypatched `stage_graph`/
# `stage_and_finalize_policy_system_leg` into raw no-op stubs (behind two
# now-removed `# detroit-exception:` comments citing this same file's
# staging convention as precedent) -- an AC-BI-004 violation confirmed by
# VERIFY_B.md, since the cited precedent had already been upgraded to this
# real fixture and the fix never propagated back here. This fixture lets
# `stage_graph`/`stage_and_finalize_policy_system_leg`/`raw_connection` all
# run FOR REAL: a real `GRAPH.COPY` snapshot and a real WATCH-guarded
# `RENAME` finalize against this in-memory graph-key registry, not a
# scripted no-op.
# --------------------------------------------------------------------------


class _FakeQueryResult:
    """Satisfies `GraphQueryResult`/the bare `.result_set` shape structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies `graph_reader._RegulatoryInstrumentNode` structurally -- only
    `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


_CREATE_NODE_RE = re.compile(r"CREATE \(n:(?P<label>\w+)\) SET n = row")
_MERGE_EDGE_RE = re.compile(
    r"MATCH \(s:(?P<source_label>\w+) \{id: row\.source_id\}\), "
    r"\(t:(?P<target_label>\w+) \{id: row\.target_id\}\) "
    r"MERGE \(s\)-\[r:(?P<rel>\w+)\]->\(t\)"
)
_COUNT_RE = re.compile(r"^MATCH \(n:(?P<label>\w+)\) RETURN count\(n\) AS c$")
_WHOLE_NODE_READ_RE = re.compile(r"^MATCH \(n:(?P<label>\w+) \{id: \$(?P<param>\w+)\}\) RETURN n$")
_PROVENANCE_READ_RE = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\)-\[e:(?P<rel>\w+)\]->"
    r"\(n:(?P<label>\w+)\) RETURN n\.id, e\.(?P<prop>\w+)$"
)
_EDGE_READ_RE = re.compile(
    r"^MATCH \(s:(?P<source_label>\w+)\)-\[:(?P<rel>\w+)\]->\(t:(?P<target_label>\w+)\) "
    r"RETURN s\.id, t\.id$"
)
_NODE_READ_RE = re.compile(
    r"^MATCH \(n:(?P<label>\w+)\)(?P<filter> WHERE n\.status = 'approved')? "
    r"RETURN (?P<cols>n\.\w+(?:, n\.\w+)*)$"
)
_VIVIFY_QUERY = "MATCH (n) WHERE false RETURN n"


class _FakeStagedGraph:
    """A generic, mutable in-memory graph store standing in for one
    FalkorDB-selected staged key (`stage_graph`'s `{short}_native`/
    `{short}_baseline` legs).

    Populated FOR REAL by `ps_service.restore.populate.populate_graph`'s two
    generic write templates (`UNWIND $rows AS row CREATE (n:{label}) SET n =
    row` / the matching edge `MERGE`), and read back FOR REAL by
    `ps_service.company_merge.graph_reader.read_baseline_graph`'s twenty-two
    fixed-literal read queries -- every one of which follows one of four
    regular shapes (a node-column projection, the whole-node RegulatoryInstrument
    read, a bare two-column edge read, or a RegulatoryInstrument-anchored
    provenance-edge read), so one regex-driven dispatcher answers all of
    them generically instead of hand-listing per-label branches. Not a
    general Cypher engine -- recognizes exactly the query shapes these two
    real modules issue, the same "structural fake, not a mock" precedent
    `_FakeBaselineGraph` (`test_restore_instrument_classification_
    passthrough.py`) already established, just backed by generically-
    populated tables instead of constructor-supplied rows.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, dict[str, object]]] = {}
        self._edges: dict[tuple[str, str, str], list[tuple[str, str, dict[str, object]]]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        params = params or {}
        if q == _VIVIFY_QUERY:
            return _FakeQueryResult([])
        if match := _CREATE_NODE_RE.search(q):
            label = match.group("label")
            table = self._nodes.setdefault(label, {})
            for row in cast("list[dict[str, object]]", params["rows"]):
                table[cast("str", row["id"])] = dict(row)
            return _FakeQueryResult([])
        if match := _MERGE_EDGE_RE.search(q):
            key = (match.group("rel"), match.group("source_label"), match.group("target_label"))
            bucket = self._edges.setdefault(key, [])
            for row in cast("list[dict[str, object]]", params["rows"]):
                bucket.append(
                    (
                        cast("str", row["source_id"]),
                        cast("str", row["target_id"]),
                        dict(cast("dict[str, object]", row["properties"])),
                    )
                )
            return _FakeQueryResult([])
        if match := _COUNT_RE.match(q):
            return _FakeQueryResult([[len(self._nodes.get(match.group("label"), {}))]])
        if match := _WHOLE_NODE_READ_RE.match(q):
            row = self._nodes.get(match.group("label"), {}).get(
                cast("str", params[match.group("param")])
            )
            if row is None:
                return _FakeQueryResult([])
            return _FakeQueryResult([[_FakeRegulatoryInstrumentNode(dict(row))]])
        if match := _PROVENANCE_READ_RE.match(q):
            # `_edges` is keyed (rel, source_label, target_label); provenance
            # edges are always sourced from RegulatoryInstrument.
            key = (match.group("rel"), "RegulatoryInstrument", match.group("label"))
            wanted_source = cast("str", params["regulatory_instrument_id"])
            prop = match.group("prop")
            return _FakeQueryResult(
                [
                    [target_id, edge_properties.get(prop)]
                    for source_id, target_id, edge_properties in self._edges.get(key, [])
                    if source_id == wanted_source
                ]
            )
        if match := _EDGE_READ_RE.match(q):
            key = (match.group("rel"), match.group("source_label"), match.group("target_label"))
            return _FakeQueryResult(
                [
                    [source_id, target_id]
                    for source_id, target_id, _props in self._edges.get(key, [])
                ]
            )
        if match := _NODE_READ_RE.match(q):
            label = match.group("label")
            approved_only = bool(match.group("filter"))
            columns = [c.split(".", 1)[1] for c in match.group("cols").split(", ")]
            rows: list[object] = []
            for row in self._nodes.get(label, {}).values():
                if approved_only and row.get("status") != "approved":
                    continue
                rows.append([row.get(column) for column in columns])
            return _FakeQueryResult(rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _FakeSingleTenantGraph:
    """Answers Capability/Policy existing-canonical-index reads and every
    `graph_writer` write query `_run_baseline_merge` issues against the
    single-tenant/snapshot graph -- mirrors `test_restore_instrument_
    classification_passthrough.py`'s own `_FakeSingleTenantGraph` exactly
    (duplicated per this test suite's per-component fake convention), plus
    a `.copy()` (`GRAPH.COPY`) method that file never needed: its tests call
    `_run_baseline_merge` directly, bypassing `staging.
    stage_and_finalize_policy_system_leg`'s own `snapshot_single_tenant`
    step, which this file's tests -- running the REAL staging orchestration
    -- do not.
    """

    def __init__(
        self,
        *,
        registry: dict[str, object] | None = None,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
        practice_area_rows: list[object] | None = None,
        risk_path_rows: list[object] | None = None,
    ) -> None:
        self._registry = registry
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows or []:
            row_list = list(cast("list[object]", row))
            self._capabilities[cast("str", row_list[0])] = row_list
        self._policies: dict[str, list[object]] = {}
        for row in policy_rows or []:
            row_list = list(cast("list[object]", row))
            self._policies[cast("str", row_list[0])] = row_list
        self._standards: dict[str, list[object]] = {}
        self._controls: dict[str, list[object]] = {}
        self._practice_areas: dict[str, dict[str, object]] = {}
        for row in practice_area_rows or []:
            row_list = list(cast("list[object]", row))
            self._practice_areas[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self._risk_paths: dict[str, dict[str, object]] = {}
        for row in risk_path_rows or []:
            row_list = list(cast("list[object]", row))
            self._risk_paths[cast("str", row_list[0])] = dict(
                cast("dict[str, object]", row_list[1])
            )
        self.calls: list[object] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append((q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(row) for row in self._policies.values()])
        if "MERGE (n:Standard {id: $id}) SET n += $properties" in q:
            self._set(self._standards, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Control {id: $id}) SET n += $properties" in q:
            self._set(self._controls, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:Capability {id: $id}) ON CREATE SET" in q:
            self._mint(self._capabilities, params, "name")
            return _FakeQueryResult([])
        if "MERGE (n:Policy {id: $id}) ON CREATE SET" in q:
            self._mint(self._policies, params, "title")
            return _FakeQueryResult([])
        if "MERGE (n:PracticeArea {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._practice_areas, params)
            return _FakeQueryResult([])
        if "MERGE (n:RiskPath {id: $id}) ON CREATE SET" in q:
            self._mint_properties(self._risk_paths, params)
            return _FakeQueryResult([])
        if "MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._capabilities, params)
            return _FakeQueryResult([])
        if "MATCH (n:Policy {id: $id}) WHERE n.embedding IS NULL" in q:
            self._backfill(self._policies, params)
            return _FakeQueryResult([])
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            assert params is not None
            requested_ids = cast("list[str]", params["ids"])
            known_ids = (
                set(self._capabilities)
                | set(self._policies)
                | set(self._standards)
                | set(self._controls)
                | set(self._practice_areas)
                | set(self._risk_paths)
            )
            return _FakeQueryResult([[rid] for rid in requested_ids if rid in known_ids])
        return _FakeQueryResult([[0]])  # any other write (RegulatoryInstrument, edges, ...)

    def copy(self, clone: str) -> object:
        """`GRAPH.COPY` stand-in: snapshot this graph's full state under `clone`.

        Registers the clone into the shared registry `_FakeStagingFalkorDB`
        passed at construction, so a later `db.select_graph(clone)` returns
        this same snapshot object -- `stage_and_finalize_policy_system_leg`'s
        own `snapshot_single_tenant` -> `run_offline_merge(snapshot_name)` ->
        `select_company_merge_graph(db, snapshot_name)` call chain.
        """
        snapshot = _FakeSingleTenantGraph(
            registry=self._registry,
            capability_rows=[list(row) for row in self._capabilities.values()],
            policy_rows=[list(row) for row in self._policies.values()],
            practice_area_rows=[[pid, dict(props)] for pid, props in self._practice_areas.items()],
            risk_path_rows=[[rid, dict(props)] for rid, props in self._risk_paths.items()],
        )
        snapshot._standards = dict(self._standards)  # same-class internal copy
        snapshot._controls = dict(self._controls)  # same-class internal copy
        if self._registry is not None:
            self._registry[clone] = snapshot
        return snapshot

    def _set(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint(
        self, table: dict[str, list[object]], params: dict[str, object] | None, text_key: str
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = [node_id, properties.get(text_key), properties.get("embedding")]

    def _mint_properties(
        self, table: dict[str, dict[str, object]], params: dict[str, object] | None
    ) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        if node_id in table:
            return
        properties = cast("dict[str, object]", params["properties"])
        table[node_id] = dict(properties)

    def _backfill(self, table: dict[str, list[object]], params: dict[str, object] | None) -> None:
        assert params is not None
        node_id = cast("str", params["id"])
        row = table.get(node_id)
        if row is None or row[2] is not None:
            return
        row[2] = params["embedding"]


class _FakeWatchablePipeline:
    """Satisfies `_WatchablePipeline` structurally: a single-writer fake, so
    `watch()` never observes a conflicting change and `execute()` always
    succeeds -- these tests exercise the SUCCESS paths only (concurrency-
    retry itself is already proven live by `test_restore_instrument_
    concurrency_live.py`).
    """

    def __init__(self, registry: dict[str, object]) -> None:
        self._registry = registry
        self._queued: list[tuple[str, str]] = []

    def watch(self, *names: str) -> None:
        del names

    def multi(self) -> None:
        pass

    def rename(self, src: str, dst: str) -> object:
        self._queued.append((src, dst))
        return None

    def execute(self) -> list[object]:
        for src, dst in self._queued:
            self._registry[dst] = self._registry.pop(src)
        self._queued = []
        return []

    def reset(self) -> None:
        self._queued = []


class _FakeRawConnection:
    """Satisfies `_RawGraphConnection` structurally, backed by the same
    registry `_FakeStagingFalkorDB.select_graph` reads/writes -- so a
    `RENAME` (finalize) or `DELETE` (discard-on-failure) is a real mutation
    of the same in-memory graph-key store every `select_graph` call sees.
    """

    def __init__(self, registry: dict[str, object]) -> None:
        self._registry = registry

    def rename(self, src: str, dst: str) -> bool:
        self._registry[dst] = self._registry.pop(src)
        return True

    def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self._registry:
                del self._registry[name]
                deleted += 1
        return deleted

    def pipeline(self, *, transaction: bool = True) -> _FakeWatchablePipeline:
        del transaction
        return _FakeWatchablePipeline(self._registry)


class _FakeStagingFalkorDB:
    """The `db: FalkorDB` stand-in for these tests: a real (in-memory) graph-
    key registry, so `stage_graph`/`stage_and_finalize_policy_system_leg`/
    `_run_baseline_merge`/`raw_connection` all run unmocked against it.
    """

    def __init__(self, single_tenant_graph_name: str) -> None:
        self._graphs: dict[str, object] = {}
        self._graphs[single_tenant_graph_name] = _FakeSingleTenantGraph(registry=self._graphs)
        self.connection = _FakeRawConnection(self._graphs)

    def select_graph(self, name: str) -> object:
        if name not in self._graphs:
            self._graphs[name] = _FakeStagedGraph()
        return self._graphs[name]


def _default_open_db_stub(config: ServiceConfig) -> FalkorDB:
    """The `open_db` boundary's own default: a real, in-memory FalkorDB stand-in.

    Issue #163 Slice 19: previously `lambda config: cast("FalkorDB", object())`, a value
    only viable because the staging/merge/finalize collaborators underneath it
    (`stage_graph`/`stage_and_finalize_policy_system_leg`/`raw_connection`) were themselves
    monkeypatched into raw no-op stubs -- an AC-BI-004 violation (VERIFY_B.md), since
    `tests/restore/test_restore_instrument_audit_log.py` had already replaced that exact
    pattern with a real, stateful in-memory FalkorDB-shaped test-data builder
    (`_FakeStagingFalkorDB` above), just never propagated back here. This default means every
    scenario that reaches this far now drives the REAL `stage_graph`/`stage_and_finalize_
    policy_system_leg`/`raw_connection` -- a real `GRAPH.COPY` snapshot and a real
    WATCH-guarded `RENAME` finalize against this in-memory graph-key registry, not a scripted
    no-op. `single_tenant_graph_name()` (the same real accessor
    `restore_orchestration._default_single_tenant_graph_name` resolves through, since
    `CatalogRestoreInfra` deliberately excludes that field from substitution) seeds the fake
    under the exact key name the real orchestration will look it up by. A fresh instance is
    built per call, matching the once-per-tool-call cardinality `dependencies.open_db(config)`
    is actually invoked at.
    """
    del config
    return cast("FalkorDB", _FakeStagingFalkorDB(single_tenant_graph_name()))


def _use_fake_restore_infra(
    monkeypatch: pytest.MonkeyPatch,
    transport: CuratedSourceTransport,
    *,
    open_db: Callable[[ServiceConfig], FalkorDB] | None = None,
    fetch_artifact_override: FetchArtifactCall | None = None,
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource] | None = None,
) -> None:
    """Patch only the narrowed fetch/resolve/open_db boundary; the real factory wires the
    real `restore_instrument` business logic.

    Targets `ps_service.api.restore_orchestration.build_default_restore_from_catalog_infra`
    -- the approved-boundary entry issue #163 Slice E added to
    `docs/coding-standards/approved-mock-boundaries.yaml` -- so
    `build_default_restore_from_catalog_dependencies()`'s own, unpatched call (`mcp_server.py`'s
    one call site makes it with zero arguments) still wires the real, shipped
    `restore_instrument`; only the curated-content fetch/effective-source-resolution/FalkorDB
    boundary beneath it is substituted. `open_db` defaults to `_default_open_db_stub` -- a real,
    stateful FalkorDB stand-in (issue #163 Slice 19), not an inert placeholder -- so every
    caller of this function gets a working staging/merge/finalize path for free unless it
    supplies its own `open_db` (e.g. to prove the boundary is never opened, or to force a
    connection failure).
    """
    infra = CatalogRestoreInfra(
        fetch_artifact=fetch_artifact_override or _fetch_artifact_through(transport),
        resolve_effective_source=resolve_effective_source or _resolve_effective_source_stub,
        open_db=open_db or _default_open_db_stub,
    )
    monkeypatch.setattr(
        "ps_service.api.restore_orchestration.build_default_restore_from_catalog_infra",
        lambda: infra,
    )


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_restore_instrument(instrument_id: str = _INSTRUMENT_ID) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("restore_instrument", {"instrument_id": instrument_id})
    )
    assert isinstance(result, CallToolResult)
    return result


def _set_similarity_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.83")


def _restore_log_lines(all_lines: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        line
        for line in all_lines
        if line.get("component") == "restore" and line.get("action") == "restore_instrument"
    ]


def _mcp_log_lines(all_lines: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]


# --- Slice 2.1: happy path ----------------------------------------------------


def test_happy_path_returns_accepted_response_shape_and_logs_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-002/004: the returned `instrument_id`/`stages` match
    `RestorationAcceptedResponse`'s own field-for-field shape -- the same
    summary ps-cli's `restore instrument` used to print. Also asserts the
    `mcp_interface` started/succeeded log pair carries the resolved
    principal (AC-BI-007, restore half -- partial; Slice 2.3 proves the
    failed-call case and the real-bearer-token case).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    _use_fake_restore_infra(monkeypatch, _valid_transport())

    result = _call_restore_instrument()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert [s["stage"] for s in body["stages"]] == [
        "verified",
        "staged",
        "merged_and_finalized",
    ]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = _mcp_log_lines(all_lines)
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def test_happy_path_fetches_the_artifact_from_the_curated_source_not_a_local_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002/004: the fake `fetch_artifact` HTTP transport was hit for exactly
    `manifest.json`/`baseline.json`/`native.json` under the instrument's own
    path -- proving the fetch, not a local `catalog_repo` read, supplied the
    artifact (no local-machine dependency).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    transport = _valid_transport()
    _use_fake_restore_infra(monkeypatch, transport)

    result = _call_restore_instrument()

    assert result.is_error is False
    requested_filenames = {req.full_url.rsplit("/", 1)[-1] for req in transport.requests}
    assert requested_filenames == {"manifest.json", "baseline.json", "native.json"}
    for req in transport.requests:
        assert f"/{_INSTRUMENT_ID}/" in req.full_url


def test_happy_path_calls_the_restore_delegate_with_the_resolved_principal_as_actor(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """The REAL `restore_instrument`'s own D14 audit log entries carry `caller` equal to the
    resolved principal (D-RESTORE-DELEGATE's `actor=principal or "unknown"` convention) --
    a stronger proof than a fake delegate's own recorded kwarg, since it proves the actor
    genuinely reached the real function's own logging, not just a spy.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    _use_fake_restore_infra(monkeypatch, _valid_transport())

    result = _call_restore_instrument()

    assert result.is_error is False
    emitter.flush()
    restore_lines = _restore_log_lines(read_lines(resolve_default_log_path()))
    assert [line["outcome"] for line in restore_lines] == ["started", "succeeded"]
    assert all(line.get("caller") == LOCAL_TEST_PRINCIPAL_ID for line in restore_lines)


# --- Slice 2.1: validation (D-INSTRUMENT-ID-STRICTNESS) ------------------------


def test_malformed_instrument_id_is_rejected_at_the_schema_layer_before_the_body_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-INSTRUMENT-ID-STRICTNESS: the combined `Field(pattern=...)` +
    `AfterValidator(_reject_path_traversal_segment)` schema (see
    `mcp_server.py`'s own comment on why this is two validators, not one
    regex: pydantic-core's regex backend has no look-around support) rejects
    a leading hyphen, a forward slash, and two `".."`-containing values --
    the same cases `test_parser.py`'s own `_instrument_id_type` tests cover
    -- before `restore_instrument`'s body ever runs (the fetch transport
    below is never called). A bare in-process `server.call_tool` (this
    module's own convention throughout) propagates that schema rejection as
    a raised `ToolError`, not a returned `CallToolResult(is_error=True)` --
    confirmed by reading `MCPServer.call_tool`'s own body, which skips the
    `_handle_call_tool` wire-level handler's `except Exception ->
    CallToolResult` translation that a real transport call goes through.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()

    class _NeverCalledTransport:
        def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
            _ = (request, timeout)
            message = "must not be called for a schema-rejected call"
            raise AssertionError(message)

    _use_fake_restore_infra(monkeypatch, _NeverCalledTransport())

    for bad_instrument_id in ("-leading-hyphen", "has/slash", "../etc/passwd", "a/../b"):
        with pytest.raises(ToolError):
            _call_restore_instrument(bad_instrument_id)


# --- Slice 2.2 auth infra: mirrors test_get_catalog_listing_tool.py's/
# test_ingest_regulation_tool.py's own pattern exactly ------------------------


def _auth_context(provider: MockOidcProvider) -> AuthContext:
    return AuthContext(
        issuer=provider.issuer,
        audience="ps-service",
        cli_client_id=None,
        scopes=(),
        jwks_uri=provider.jwks_uri,
        allowed_algorithms=_ALLOWED_ALGORITHMS,
    )


def _authenticated_test_client(*, auth_context: AuthContext, host: str = "127.0.0.1") -> TestClient:
    """A `TestClient` over the real, auth-armed `build_streamable_http_app`.

    Mirrors `test_get_catalog_listing_tool.py`'s/`test_ingest_regulation_tool
    .py`'s own `_authenticated_test_client` exactly -- this module's Slice
    2.3 tests drive `restore_instrument` (not `cypher`) through the same
    genuinely auth-enforcing mounted app, never the bypass-shaped one every
    other test in this module builds via a bare `server.call_tool`.
    """
    verifier = PsTokenVerifier(auth_context)
    mcp_asgi_app = build_streamable_http_app(
        host=host, verifier=verifier, auth_context=auth_context
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            yield

    wrapper_app = Starlette(lifespan=lifespan)
    wrapper_app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)
    return TestClient(wrapper_app, base_url=_BASE_URL)


def _sse_result_text(response_text: str) -> str:
    """Extract the tool call's text content out of an SSE-formatted response body."""
    for line in response_text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            result = cast("dict[str, object]", payload["result"])
            content = cast("list[object]", result["content"])
            block = cast("dict[str, object]", content[0])
            text = block["text"]
            assert isinstance(text, str)
            return text
    msg = f"no 'data:' line found in SSE body: {response_text!r}"
    raise AssertionError(msg)


def _call_restore_instrument_over_http(client: TestClient, *, token: str) -> str:
    """Drive `restore_instrument` through the real mounted transport with a
    bearer token: `initialize` -> `notifications/initialized` -> `tools/call`,
    the same JSON-RPC sequence `test_mcp_auth.py` already establishes.
    """
    init_response = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-restore-instrument-auth-client",
                    "version": "0.0.1",
                },
            },
        },
        headers={"Accept": _JSON_RPC_ACCEPT, "Authorization": f"Bearer {token}"},
    )
    assert init_response.status_code == 200
    session_id = init_response.headers["mcp-session-id"]

    notified = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={
            "Accept": _JSON_RPC_ACCEPT,
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
        },
    )
    assert notified.status_code == 202

    call_response = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "restore_instrument",
                "arguments": {"instrument_id": _INSTRUMENT_ID},
            },
        },
        headers={
            "Accept": _JSON_RPC_ACCEPT,
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
        },
    )
    assert call_response.status_code == 200
    return _sse_result_text(call_response.text)


def _grant_compliance_officer(
    monkeypatch: pytest.MonkeyPatch, *, subject: str, issuer: str
) -> None:
    """Issue #145: seed a `ComplianceOfficer` grant for `(subject, issuer)` on a fake store,
    monkeypatched onto `mcp_server.PsycopgAccessRoleStore`.

    The real-verified-token test below predates issue #145's authz gate (it
    was written to prove principal/actor threading, not authorization) --
    without this, the newly-added gate now denies it, since the token's own
    subject holds no grant on the real store, which itself is unreachable in
    this test environment (`PS_STATE_POSTGRES_HOST` unset) and would
    otherwise fail closed. Mirrors `test_catalog_source_authz_gate.py`'s own
    `_fake_store_factory` pattern.
    """
    store = FakeAccessRoleStore()
    store.grant(
        actor=(subject, issuer), target=(subject, issuer), access_role=AccessRole.COMPLIANCE_OFFICER
    )

    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


def test_real_verified_token_principal_threads_through_to_audit_log_and_delegate_actor(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-001/007: under a real, verified bearer token -- driven through
    the actual mounted Streamable HTTP transport, mirroring
    `test_get_catalog_listing_tool.py`'s own pattern, never a bare in-process
    `server.call_tool` -- the token's `sub` claim (never
    `LOCAL_TEST_PRINCIPAL_ID`) reaches both the `mcp_interface` log entries'
    `principal` AND the real `restore_instrument`'s own D14 `caller` field.

    Issue #145: the caller must also hold `ComplianceOfficer` now that this
    tool is gated, so `_grant_compliance_officer` seeds that grant for this
    token's own `(sub, iss)` -- orthogonal to what this test itself proves.
    """
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-restore-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    _use_fake_restore_infra(monkeypatch, _valid_transport())

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_restore_instrument_over_http(client, token=token)

    body = json.loads(result_text)
    assert body["instrument_id"] == _INSTRUMENT_ID

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = _mcp_log_lines(all_lines)
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    assert not any(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)

    restore_lines = _restore_log_lines(all_lines)
    assert [line["outcome"] for line in restore_lines] == ["started", "succeeded"]
    assert all(line.get("caller") == token_sub for line in restore_lines)


def test_local_test_bypass_principal_still_threads_through_when_no_token_is_presented(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-001: `_resolve_principal`'s existing, unchanged fallback
    contract -- with no bearer token presented at all (a bare in-process
    `server.call_tool`) and the local-test bypass active,
    `LOCAL_TEST_PRINCIPAL_ID` still reaches the `mcp_interface` log entries'
    `principal` (already proven by Slice 2.1's own test; re-proven here as
    this slice's own dedicated auth/audit completeness check, mirroring
    `test_get_catalog_listing_tool.py`'s identically-purposed test).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    _use_fake_restore_infra(monkeypatch, _valid_transport())

    result = _call_restore_instrument()

    assert result.is_error is False
    emitter.flush()
    mcp_lines = _mcp_log_lines(read_lines(resolve_default_log_path()))
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)


def test_failed_call_under_a_real_verified_token_still_carries_the_subs_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-007: a FAILED call -- a real checksum-mismatched artifact -- still carries the
    resolved real-token principal on its `outcome="failed"` `mcp_interface` log entry, not
    only the succeeded-call case proven above.
    """
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-restore-failed-7"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    _use_fake_restore_infra(monkeypatch, _transport_with(baseline_sha256="0" * 64))

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_restore_instrument_over_http(client, token=token)

    assert result_text.startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = _mcp_log_lines(all_lines)
    assert [line["outcome"] for line in mcp_lines] == ["started", "failed"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)


# --- Slice 2.2: failure-state completeness -------------------------------------


def test_curated_source_unreachable_returns_named_error_and_never_calls_the_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-RESTORE: an unreachable curated-content source
    (`CuratedSourceUnavailableError`, raised by `run_restoration_from_catalog_source`
    when `fetch_artifact` raises `CuratedSourceFetchError`) is caught and
    returned as `error: <str(exc)>` verbatim -- the FalkorDB boundary is
    never opened (the restore delegate is never reached).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    open_db_calls: list[object] = []

    def _counting_open_db(config: ServiceConfig) -> FalkorDB:
        open_db_calls.append(config)
        return cast("FalkorDB", object())

    _use_fake_restore_infra(
        monkeypatch, FakeFailingCuratedSourceTransport(), open_db=_counting_open_db
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert text != "error: an unexpected error occurred"
    assert open_db_calls == []


def test_override_read_failure_returns_named_error_and_never_fetches_or_opens_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-010: `CatalogSourceOverrideUnavailableError` (the fail-closed override read) is
    returned as its fixed `error:` string -- neither the default source nor FalkorDB is used.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    open_db_calls: list[object] = []
    transport = _transport_with()

    def _counting_open_db(config: ServiceConfig) -> FalkorDB:
        open_db_calls.append(config)
        return cast("FalkorDB", object())

    def _failing_resolve(config: ServiceConfig) -> EffectiveCatalogSource:
        del config
        raise RuntimeConfigUnavailableError("driver said: 10.0.0.5:5432 refused")

    _use_fake_restore_infra(
        monkeypatch,
        transport,
        open_db=_counting_open_db,
        resolve_effective_source=_failing_resolve,
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result).startswith("error: ")
    assert "5432" not in _text(result)
    assert _text(result) != "error: an unexpected error occurred"
    assert transport.requests == []
    assert open_db_calls == []


def test_checksum_mismatch_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: the fetched artifact's checksum doesn't match its
    own manifest (real `ArtifactIntegrityError` from the real `restore_instrument`,
    zero FalkorDB calls -- D9 runs before any) -> `RestoreArtifactRejectedError`,
    returned as `error: <str(exc)>`.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    _use_fake_restore_infra(monkeypatch, _transport_with(baseline_sha256="0" * 64))

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "checksum mismatch" in text


def test_schema_version_mismatch_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: real `ArtifactSchemaVersionMismatchError` from the real
    `restore_instrument` (D10, zero FalkorDB calls) -> `RestoreArtifactRejectedError`,
    returned verbatim.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    bad_schema_version = "not-" + DOMAIN_SCHEMA_VERSION
    _use_fake_restore_infra(monkeypatch, _transport_with(schema_version=bad_schema_version))

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "schema_version" in text


def test_restore_stage_failure_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: any other restore-stage failure -- here, a real
    `ArtifactContentRejectedError` from the real `restore_instrument`'s own
    content-validation step (GH #104, a native leg carrying a label outside
    the allow-list) -> `RestoreStageFailedError`, returned as `error:
    <str(exc)>` naming the failing stage.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    _use_fake_restore_infra(monkeypatch, _content_violating_transport())

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert text != "error: an unexpected error occurred"


def test_missing_similarity_threshold_returns_named_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-RESTORE: `_require_similarity_threshold` raises
    `RestoreStageFailedError(stage="configuration", ...)` directly, outside
    any try/except inside `run_restoration_from_catalog_source`, when
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` is unset -- reaches the tool the
    same uncaught way, and is still caught by this tool's own
    `RestoreStageFailedError` handler. The FalkorDB boundary is never
    opened -- this check runs before `dependencies.open_db` is ever called.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)
    configure()
    open_db_calls: list[object] = []

    def _counting_open_db(config: ServiceConfig) -> FalkorDB:
        open_db_calls.append(config)
        return cast("FalkorDB", object())

    _use_fake_restore_infra(monkeypatch, _valid_transport(), open_db=_counting_open_db)

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "PS_COMPANYMERGE_SIMILARITY_THRESHOLD" in text
    assert open_db_calls == []


def test_graph_unavailable_returns_generic_graph_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: `dependencies.open_db` raising any exception is
    wrapped by `_sanitize_restore_graph_opens` into `McpGraphUnavailableError`,
    surfaced as the shared, fixed `_GRAPH_UNAVAILABLE_MESSAGE` -- never the
    raw driver exception.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()

    def _raising_open_db(config: ServiceConfig) -> FalkorDB:
        _ = config
        message = "connection refused -- must never reach the caller"
        raise ConnectionRefusedError(message)

    _use_fake_restore_infra(monkeypatch, _valid_transport(), open_db=_raising_open_db)

    result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4: an exception the tool body does not itself
    sanitise (here, `dependencies.fetch_artifact` raising something
    unclassified, not `CuratedSourceFetchError`) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()

    def _raising_fetch_artifact(base_url: str, instrument_id: str) -> FetchedArtifact:
        _ = (base_url, instrument_id)
        message = "boom -- must never reach the caller"
        raise ValueError(message)

    _use_fake_restore_infra(
        monkeypatch, _valid_transport(), fetch_artifact_override=_raising_fetch_artifact
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    lines = _mcp_log_lines(read_lines(resolve_default_log_path()))
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "ValueError" in str(failed_line.get("detail"))
