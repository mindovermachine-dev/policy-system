"""Tests for the registered `ingest_regulation` MCP tool (issue #126, Slices 1.1-1.3).

Slice 1.1 covers the curated-CELEX happy path: `find_by_celex` resolves a real
catalog entry, the caller-supplied `short_name` matches the catalog's own
value, and the tool delegates in-process to `run_catalog_ingestion_pipeline`
(D-DELEGATE) via the new `_run_mcp_action` audit wrapper (D-AUDIT-WRAPPER),
returning `routes._to_accepted_response(...).model_dump()` (D-RESPONSE-SHAPE).

Slice 1.2 covers the non-curated (Cellar-fallback) path -- the actual #96 fix
(CHANGES.md C1/Appendix C1): when `find_by_celex` returns `None`, the tool
calls `resolve_via_cellar(celex, short_name=short_name)` with the
caller-supplied, already-pattern-validated `short_name`, which is used
verbatim -- `_derive_short_name` is never reached on this path, so two
resolutions of the same CELEX with differently-worded Cellar titles no
longer diverge into two different `short_name`s/graph names.

Slice 1.3 wires the remaining D-SANITIZE-UNEXPECTED error-taxonomy rows for
this tool: `IngestionConfigIncompleteError`, `PipelineStageError`, the
D-PREFLIGHT LLM-Interface-unhealthy check, the graph-unavailable sanitiser
around each of `run_catalog_ingestion_pipeline`'s three graph-open call
sites, and `_run_mcp_action`'s own residual unexpected-exception safety net
(D-AUDIT-WRAPPER point 4), proven here specifically for `ingest_regulation`.

Slice 1.4 proves D-AUTH's auth/audit-completeness claim end to end under a
*real, verified* bearer token -- not just the local-test-bypass principal
every test above exercises via a bare in-process `server.call_tool`. It
drives the actual mounted Streamable HTTP transport against the shared
`MockOidcProvider` test infra, mirroring `test_mcp_auth.py`'s own
`_authenticated_test_client`/JSON-RPC-sequence pattern (reused/mirrored, not
reinvented), proving: (a) a verified token's `sub` reaches both
`_run_mcp_action`'s `mcp_interface` log entries (`principal`) and
`run_catalog_ingestion_pipeline`'s own `ingestion_run` log entries
(`caller`); (b) the local-test-bypass fallback still threads
`LOCAL_TEST_PRINCIPAL_ID` through both of those same log entries when no
token is presented; (c) a **failed** call under a real token still carries
that token's principal on its `outcome="failed"` log entry.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_cypher_tool.py`'s own convention. The fake `PipelineDependencies`
bundle (`GraphOpeners`/`PipelineStages`/`PipelineAdapters`) is the existing
`tests/api/_fakes.py::build_fake_pipeline_dependencies` fixture already used
by the REST-side `run_catalog_ingestion_pipeline` tests
(`tests/api/test_ingestion_orchestration.py`) -- reused here unchanged, not
reinvented, per PLAN.md's own instruction.

`pytest-asyncio` is not installed; most of this file's tests drive the tool
with bare `asyncio.run(server.call_tool(...))`, exactly like
`test_cypher_tool.py`. Slice 1.4's real-token tests instead drive the real
mounted ASGI transport through `TestClient`, since a bare `server.call_tool`
never populates the `get_access_token()` contextvar a real token requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest
from api._fakes import FakeGraphHandle, build_fake_pipeline_dependencies
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- issue #145: same cross-package import `test_catalog_source_authz_gate.py` already establishes, reused here so this file's own two real-token tests can grant the caller `ComplianceOfficer` on the new gate
    FakeAccessRoleStore,
)
from fastapi.testclient import TestClient
from litellm.types.utils import Choices, Embedding, EmbeddingResponse, Message, ModelResponse
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from ps_service import dependency_health
from ps_service.api.catalog import CatalogEntry, find_by_celex
from ps_service.api.errors import ShortNameCollisionError
from ps_service.api.ingestion_orchestration import (
    _MERGED_INSTRUMENT_EXISTS_QUERY,  # pyright: ignore[reportPrivateUsage]  -- query-text dispatch key, mirrors tests/api/_fakes.py's own precedent for cross-module private reuse
    _SHORT_NAME_COLLISION_QUERY,  # pyright: ignore[reportPrivateUsage]  -- query-text dispatch key
    GraphOpeners,
    PipelineAdapters,
    PipelineDependencies,
    PipelineStages,
    validate_and_resolve_catalog_entry,
)
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.authz.models import AccessRole
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.models import ExtractionUnit
from ps_service.domain_mapper.prompts import (
    CAPABILITY_DERIVATION_SYSTEM_PROMPT,
    DEFINITIONS_EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    OBLIGATION_DERIVATION_SYSTEM_PROMPT,
)
from ps_service.ingestion.adapters.errors import CellarNotFoundError
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from ps_service.company_merge.models import MergeResult
    from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_test_support.mock_oidc_provider import MockOidcProvider

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})


def _curated_cra_entry() -> CatalogEntry:
    """A real curated catalog entry, so this test never hand-duplicates the
    catalog's own celex/short_name/version values.
    """
    entry = find_by_celex("32024R2847")
    assert entry is not None, "fixture assumption: CRA is a curated catalog entry"
    return entry


_CURATED_ENTRY = _curated_cra_entry()
_CELEX = _CURATED_ENTRY.celex
_SHORT_NAME = _CURATED_ENTRY.short_name
_VERSION = _CURATED_ENTRY.version
_RID = f"{_SHORT_NAME}-{_VERSION}"

# --- Slice 1.2 fixtures: a CELEX absent from the curated catalog, resolved via
# Cellar/ELI -- mirrors tests/api/test_ingestion_orchestration.py's own
# `_NONCURATED_CELEX`/Fixture-A/RDF-fixture shapes, not reinvented.
_NONCURATED_CELEX = "32020R1111"

_RDF_FIXTURE_REGULATION_A = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32020R1111">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32020R1111</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2030-01-01</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""


def _xhtml_fixture(title: str) -> bytes:
    """A Regulation-shaped Cellar XHTML fixture carrying `title` as its main title."""
    return f"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">{title}</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
""".encode()


def _configure_complete_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the three config fields `_require_ingestion_config` needs to be non-`None`."""
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", "azure/gpt-4o")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.83")


def _call_ingest_regulation(celex: str, short_name: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("ingest_regulation", {"celex": celex, "short_name": short_name})
    )
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- issue #163 Slice C: the two ways this module supplies pipeline
# dependencies to the `ingest_regulation` tool, now that
# `build_default_pipeline_dependencies`'s own DI seam is narrowed to
# `graphs` only -- `PipelineStages`/`PipelineAdapters` are always the real,
# shipped functions when that factory itself runs unpatched. -----------------


def _use_fake_graph_openers_only(monkeypatch: pytest.MonkeyPatch, graphs: GraphOpeners) -> None:
    """Patch only the narrowed FalkorDB boundary; the real factory wires real stages.

    Targets `ps_service.api.ingestion_orchestration.build_default_graph_openers`
    -- the approved-boundary entry issue #163 Slice C added to
    `docs/coding-standards/approved-mock-boundaries.yaml` -- so
    `_resolve_and_ingest`'s own, unpatched `build_default_pipeline_dependencies()`
    call still wires the real `PipelineStages`/`PipelineAdapters`. Every
    caller of this helper is a test whose own scenario is rejected or fails
    before any pipeline stage is ever reached (each asserts
    `fake.recorder.calls == []`, or never calls `_call_ingest_regulation` at
    all), so which stage functions this wiring exposes -- real, since
    nothing overrides them any more -- is moot: only `graphs`'s own scripted
    open/query behaviour is exercised.
    """
    monkeypatch.setattr(
        "ps_service.api.ingestion_orchestration.build_default_graph_openers", lambda: graphs
    )


# --- issue #163 Slice 20: run the REAL four pipeline stage functions end to
# end, replacing the wholesale `Fake*Stage` substitution VERIFY_B.md flagged
# as an unfixed AC-BI-003 violation (no interaction was ever asserted at that
# seam; it substituted real Domain Mapper/Company Merge/Ingestion engine
# logic wholesale, not just an infra boundary). `_MiniGraph` below is a
# generic, stateful in-memory FalkorDB-shaped graph double -- the same
# "structural fake, not a mock" precedent `test_restore_instrument_audit_
# log.py`'s `_FakeStagedGraph` (IMPL_SLICE_8.md) and `test_export_instrument_
# cli.py`'s `_StatefulFakeGraph` (IMPL_SLICE_13.md) already established,
# generalized here across the generic Cypher shapes `ps_service.ingestion.
# graph_writer`, `ps_service.domain_mapper.{extraction,graph_writer,
# derivation}`, and `ps_service.company_merge.{graph_reader,graph_writer,
# dedup}` each issue (MERGE-node upsert with/without `ON CREATE`, MATCH+MERGE
# two-endpoint edge upsert with/without one `SET` edge property, node count,
# RegulatoryInstrument-anchored `HAS*1..` reachability, and the fixed-literal
# reads `read_baseline_graph`/`read_existing_canonical_index` issue) rather
# than one component's own fixed literal query list -- not a general Cypher
# engine, only what these components' own writers/readers actually emit.
# See `.orchestrator/tracker/issue-163/IMPL_SLICE_20.md` for the full
# rationale and for the one remaining exception this slice could not close.


class _FakeQueryResult:
    """Satisfies the orchestration's `_QueryResult`/`GraphQueryResult` Protocols."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraphNode:
    """Structural stand-in for a `falkordb.Node` -- only `.properties` is ever read."""

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


_NODE_WRITE_RE = re.compile(
    r"^MERGE \(n:(?P<label>\w+) \{id: \$id\}\) (?P<mode>ON CREATE SET|SET) n \+= \$properties$"
)
_EDGE_WRITE_RE = re.compile(
    r"^MATCH \((?P<a_var>\w+):(?P<a_label>\w+) \{id: \$(?P<a_param>\w+)\}\), "
    r"\((?P<b_var>\w+):(?P<b_label>\w+) \{id: \$(?P<b_param>\w+)\}\) "
    r"MERGE \((?P=a_var)\)-\[(?:\w+)?:(?P<rel>\w+)\]->\((?P=b_var)\)"
    r"(?: SET \w+\.(?P<eprop>\w+) = \$(?P<eprop_param>\w+))?$"
)
_EMBEDDING_BACKFILL_RE = re.compile(
    r"^MATCH \(n:(?P<label>\w+) \{id: \$id\}\) WHERE n\.embedding IS NULL "
    r"SET n\.embedding = \$embedding$"
)
_COUNT_RE = re.compile(r"^MATCH \(n:(?P<label>\w+)\) RETURN count\(n\)$")
_REACHABILITY_RE = re.compile(
    r"^MATCH \(n:(?P<label>\w+)\) WHERE NOT \(:RegulatoryInstrument\)-\[:HAS\*1\.\.\]->\(n\) "
    r"RETURN count\(n\)$"
)
_WHOLE_NODE_READ_RE = re.compile(r"^MATCH \(n:(?P<label>\w+) \{id: \$(?P<param>\w+)\}\) RETURN n$")
_NODE_READ_RE = re.compile(
    r"^MATCH \(n:(?P<label>\w+)\)(?P<filter> WHERE n\.status = 'approved')? "
    r"RETURN (?P<cols>n\.\w+(?:, n\.\w+)*)$"
)
_PROVENANCE_READ_RE = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\)-\[e:(?P<rel>\w+)\]->"
    r"\(n:(?P<label>\w+)\) RETURN n\.id, e\.(?P<prop>\w+)$"
)
_EDGE_READ_RE = re.compile(
    r"^MATCH \(s:(?P<source_label>\w+)\)-\[:(?P<rel>\w+)\]->\(t:(?P<target_label>\w+)\) "
    r"RETURN s\.id, t\.id$"
)
_RI_PLAIN_READ_QUERY = "MATCH (r:RegulatoryInstrument) RETURN r"
_DERIVATION_READ_QUERY = (
    "MATCH (req:Requirement) "
    "OPTIONAL MATCH (rl:Role {id: req.role_id}) "
    "RETURN req.id, req.text, req.role_id, rl.id, rl.name"
)


class _MiniGraph:
    """A generic, mutable in-memory graph standing in for one selected FalkorDB graph.

    One instance plays each of `native`/`baseline`/`single_tenant` -- the
    real four pipeline stage functions never learn they're not talking to a
    real FalkorDB graph. Nodes are stored `{label: {id: {**properties,
    "id": id}}}` (a real `MERGE (n:{label} {id: $id}) SET n += $properties`
    genuinely leaves `id` itself as a readable property, since it was bound
    by the `MERGE` pattern -- `domain_mapper.graph_writer`'s own module
    docstring documents exactly this, see its "BASELINE.md row #2 fix" note)
    so a later whole-node `RETURN n`/column-projection read can recover it
    generically, without per-label special-casing. Edges are stored keyed by
    `(relationship_type, source_label, target_label)` -> `{(source_id,
    target_id): edge_properties}`; a `MATCH ... MERGE` edge write whose
    endpoint MATCH resolves nothing is a genuine real-Cypher no-op (no
    exception), mirrored here as a silent no-op too.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, dict[str, dict[str, object]]] = {}
        self._edges: dict[tuple[str, str, str], dict[tuple[str, str], dict[str, object]]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        params = params or {}
        if q == _RI_PLAIN_READ_QUERY:
            table = self._nodes.get("RegulatoryInstrument", {})
            if not table:
                return _FakeQueryResult([])
            return _FakeQueryResult([[_FakeGraphNode(dict(next(iter(table.values()))))]])
        if q == _DERIVATION_READ_QUERY:
            return self._read_requirements_by_role()
        if q == _MERGED_INSTRUMENT_EXISTS_QUERY:
            node_id = cast("str", params["id"])
            exists = node_id in self._nodes.get("RegulatoryInstrument", {})
            return _FakeQueryResult([[node_id]] if exists else [])
        if q == _SHORT_NAME_COLLISION_QUERY:
            prefix = cast("str", params["prefix"])
            celex = params["celex"]
            return _FakeQueryResult(
                [
                    [node_id, node.get("celex")]
                    for node_id, node in self._nodes.get("RegulatoryInstrument", {}).items()
                    if node_id.startswith(prefix)
                    and node.get("celex") is not None
                    and node.get("celex") != celex
                ]
            )
        if match := _NODE_WRITE_RE.match(q):
            self._write_node(match["label"], match["mode"], params)
            return _FakeQueryResult([])
        if match := _EDGE_WRITE_RE.match(q):
            self._write_edge(match, params)
            return _FakeQueryResult([])
        if match := _EMBEDDING_BACKFILL_RE.match(q):
            self._backfill_embedding(match["label"], params)
            return _FakeQueryResult([])
        if match := _COUNT_RE.match(q):
            return _FakeQueryResult([[len(self._nodes.get(match["label"], {}))]])
        if match := _REACHABILITY_RE.match(q):
            return _FakeQueryResult([[self._count_unreachable(match["label"])]])
        if match := _WHOLE_NODE_READ_RE.match(q):
            node = self._nodes.get(match["label"], {}).get(cast("str", params[match["param"]]))
            if node is None:
                return _FakeQueryResult([])
            return _FakeQueryResult([[_FakeGraphNode(dict(node))]])
        if match := _PROVENANCE_READ_RE.match(q):
            key = (match["rel"], "RegulatoryInstrument", match["label"])
            wanted_source = cast("str", params["regulatory_instrument_id"])
            prop = match["prop"]
            return _FakeQueryResult(
                [
                    [target_id, edge_properties.get(prop)]
                    for (source_id, target_id), edge_properties in self._edges.get(key, {}).items()
                    if source_id == wanted_source
                ]
            )
        if match := _EDGE_READ_RE.match(q):
            key = (match["rel"], match["source_label"], match["target_label"])
            return _FakeQueryResult(
                [[source_id, target_id] for source_id, target_id in self._edges.get(key, {})]
            )
        if match := _NODE_READ_RE.match(q):
            label = match["label"]
            approved_only = bool(match["filter"])
            columns = [c.split(".", 1)[1] for c in match["cols"].split(", ")]
            rows: list[object] = []
            for node in self._nodes.get(label, {}).values():
                if approved_only and node.get("status") != "approved":
                    continue
                rows.append([node.get(column) for column in columns])
            return _FakeQueryResult(rows)
        raise AssertionError(f"unexpected query issued against the fake pipeline graph: {q!r}")

    def has_node(self, label: str, node_id: str) -> bool:
        """Whether a node with this exact `(label, id)` was ever written -- a
        test-observable proof that a real stage genuinely wrote real state,
        without reaching into this class's own private storage.
        """
        return node_id in self._nodes.get(label, {})

    def is_empty(self) -> bool:
        """Whether this graph has had no node or edge written to it at all."""
        return not self._nodes and not self._edges

    def _write_node(self, label: str, mode: str, params: dict[str, object]) -> None:
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        table = self._nodes.setdefault(label, {})
        existing = table.get(node_id)
        if existing is None:
            table[node_id] = {"id": node_id, **properties}
            return
        if mode == "SET":  # "ON CREATE SET" leaves an already-existing node untouched.
            existing.update(properties)

    def _write_edge(self, match: re.Match[str], params: dict[str, object]) -> None:
        a_id = cast("str", params[match["a_param"]])
        b_id = cast("str", params[match["b_param"]])
        if a_id not in self._nodes.get(match["a_label"], {}) or b_id not in self._nodes.get(
            match["b_label"], {}
        ):
            return  # the MATCH resolved nothing -- MERGE never fires, no error (real semantics)
        key = (match["rel"], match["a_label"], match["b_label"])
        edge_properties = self._edges.setdefault(key, {}).setdefault((a_id, b_id), {})
        if match["eprop"]:
            edge_properties[match["eprop"]] = params[match["eprop_param"]]

    def _backfill_embedding(self, label: str, params: dict[str, object]) -> None:
        node = self._nodes.get(label, {}).get(cast("str", params["id"]))
        if node is not None and node.get("embedding") is None:
            node["embedding"] = params["embedding"]

    def _count_unreachable(self, label: str) -> int:
        reachable: set[str] = set()
        frontier = set(self._nodes.get("RegulatoryInstrument", {}))
        while frontier:
            reachable |= frontier
            next_frontier: set[str] = set()
            for (rel, _a_label, _b_label), pairs in self._edges.items():
                if rel != "HAS":
                    continue
                for source_id, target_id in pairs:
                    if source_id in frontier and target_id not in reachable:
                        next_frontier.add(target_id)
            frontier = next_frontier
        return sum(1 for node_id in self._nodes.get(label, {}) if node_id not in reachable)

    def _read_requirements_by_role(self) -> _FakeQueryResult:
        roles = self._nodes.get("Role", {})
        rows: list[object] = []
        for requirement_id, requirement in self._nodes.get("Requirement", {}).items():
            role_id = requirement.get("role_id")
            role = roles.get(cast("str", role_id)) if role_id is not None else None
            rows.append(
                [
                    requirement_id,
                    requirement.get("text"),
                    role_id,
                    role.get("id") if role is not None else None,
                    role.get("name") if role is not None else None,
                ]
            )
        return _FakeQueryResult(rows)


def _mini_graph_openers(
    native: _MiniGraph, baseline: _MiniGraph, single_tenant: _MiniGraph
) -> GraphOpeners:
    def _open_native(config: object, short_name: str) -> _MiniGraph:
        _ = (config, short_name)
        return native

    def _open_baseline(config: object, short_name: str) -> _MiniGraph:
        _ = (config, short_name)
        return baseline

    def _open_single_tenant(config: object) -> _MiniGraph:
        _ = config
        return single_tenant

    return GraphOpeners(
        native=_open_native, baseline=_open_baseline, single_tenant=_open_single_tenant
    )


_FIXTURE_UNIT = ExtractionUnit(
    citation_ref="Art. 1",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="1",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)


def _structural_fixture(identifier: str) -> FetchedRegulatoryInstrumentStructure:
    """A minimal, real, two-node structural tree: one Chapter, one Article.

    Anchored to the RegulatoryInstrument the same way a real Cellar/ELI
    fetch would (a `RegulatoryInstrument`-anchored top edge, then a
    Chapter -> Article edge), so `verify_structural_graph_reachable`'s real
    reachability check genuinely passes against `_MiniGraph`.
    """
    chapter_id = f"{identifier}#cpt_I"
    article_id = f"{identifier}#art_1"
    return FetchedRegulatoryInstrumentStructure(
        metadata=RegulatoryInstrumentMetadata(
            title="Fixture Regulation",
            jurisdiction="EU",
            effective_date=date(2024, 1, 1),
            version="1.0",
            status="active",
            source_type="external",
            instrument_type="regulation",
            celex=identifier,
        ),
        nodes=(
            StructuralNode(
                element_type="CHAPTER", id=chapter_id, properties={"heading": "General provisions"}
            ),
            StructuralNode(
                element_type="ARTICLE",
                id=article_id,
                properties={"text": _FIXTURE_UNIT.text, "citation_ref": _FIXTURE_UNIT.citation_ref},
            ),
        ),
        edges=(
            StructuralEdge(
                parent_element_type="RegulatoryInstrument",
                parent_id=identifier,
                child_element_type="CHAPTER",
                child_id=chapter_id,
            ),
            StructuralEdge(
                parent_element_type="CHAPTER",
                parent_id=chapter_id,
                child_element_type="ARTICLE",
                child_id=article_id,
            ),
        ),
    )


class _RecordingIngestionAdapter:
    """The real `IngestionAdapter` boundary, faked: a fixed, real structural
    fetch result (no HTTP), recording that the ingest stage reached it.
    """

    def __init__(self, stage_order: list[str]) -> None:
        self._stage_order = stage_order

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        self._stage_order.append("ingestion")
        return _structural_fixture(identifier)


class _RecordingDomainMappingAdapter:
    """The real `DomainMappingAdapter` boundary, faked: a fixed unit list (no
    real native-graph parse), recording that the extract stage reached it --
    or, when `error` is set, raising it instead (a real fault-injection seam
    for `test_pipeline_stage_error_surfaces_verbatim_with_sanitised_reason`,
    not a business-logic substitution: `extract_roles_and_requirements`
    itself still runs for real up to and including this call).
    """

    def __init__(self, stage_order: list[str], *, error: Exception | None = None) -> None:
        self._stage_order = stage_order
        self._error = error

    def read_native_units(self, graph: object) -> tuple[ExtractionUnit, ...]:
        _ = graph
        self._stage_order.append("extraction")
        if self._error is not None:
            raise self._error
        return (_FIXTURE_UNIT,)


def _never_ingest_internal(*args: object, **kwargs: object) -> object:
    """`ingest_internal` is never invoked by `ingest_regulation` -- fail loudly if it is."""
    raise AssertionError(f"ingest_internal must not be called (args={args!r}, kwargs={kwargs!r})")


def _never_internal_seed_adapter() -> object:
    """The internal-seed adapter is never used by `ingest_regulation` -- fail loudly if built."""
    raise AssertionError("the internal-seed adapter must not be built by this tool")


def _model_response(content: str) -> ModelResponse:
    return ModelResponse(
        id="fixture",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def _fake_call_completion(*, fail_capability_derivation: bool = False) -> CompletionCaller:
    """Fake the one true external boundary `extract`/`derive` call through
    `route_completion` -- dispatching on the (fixed, real) system prompt each
    real stage sends, so the real extraction/obligation/capability parsers
    (`ps_service.domain_mapper.prompts`) run for real against these scripted
    responses. `fail_capability_derivation=True` (used by exactly one test)
    returns malformed JSON only for the capability-derivation prompt, forcing
    the real `_mark_capability_unmatched` path -- a legitimate, real
    unmatched-obligation outcome, not a canned result.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        _ = (model, timeout)
        system_content = messages[0]["content"]
        if system_content == EXTRACTION_SYSTEM_PROMPT:
            return _model_response(
                json.dumps(
                    {
                        "requirements": [
                            {
                                "role_name": "Manufacturer",
                                "text": _FIXTURE_UNIT.text,
                                "type": "requirement",
                                "letter_suffix": None,
                                "confidence": 0.9,
                            }
                        ]
                    }
                )
            )
        if system_content == DEFINITIONS_EXTRACTION_SYSTEM_PROMPT:
            return _model_response(json.dumps({"terms": []}))
        if system_content == OBLIGATION_DERIVATION_SYSTEM_PROMPT:
            return _model_response(
                json.dumps(
                    {
                        "matched_existing_id": None,
                        "new_text": "Conduct Cybersecurity Risk Assessment",
                        "unmatchable": False,
                        "confidence": 0.9,
                    }
                )
            )
        if system_content == CAPABILITY_DERIVATION_SYSTEM_PROMPT:
            if fail_capability_derivation:
                return _model_response("{not valid json")
            return _model_response(
                json.dumps(
                    {
                        "capabilities": [
                            {
                                "matched_existing_id": None,
                                "new_name": "Cybersecurity Risk Management",
                                "new_description": "Manage cybersecurity risk.",
                                "confidence": 0.9,
                            }
                        ]
                    }
                )
            )
        raise AssertionError(f"unexpected system prompt sent to the fake LLM: {system_content!r}")

    return _call


def _fake_call_embedding(*, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
    """Fake `route_embedding`'s boundary defensively -- unreached in every one of
    this file's scenarios (a fresh single-tenant graph's `read_existing_canonical_
    index` is always empty, so `dedup.find_best_semantic_match` short-circuits
    before ever calling this, per its own docstring), but bound anyway so a
    future scenario needing it fails on an assertion, not a real network call.
    """
    _ = timeout
    return EmbeddingResponse(
        model=model,
        data=[
            Embedding(embedding=[0.1] * 8, index=index, object="embedding")
            for index in range(len(inputs))
        ],
    )


@dataclasses.dataclass(frozen=True, slots=True)
class RealPipelineFixture:
    """Handles a test asserts against, after wiring the real four pipeline
    stage functions to run end to end against a fake, stateful graph.
    """

    stage_order: list[str]
    native: _MiniGraph
    baseline: _MiniGraph
    single_tenant: _MiniGraph


def _use_real_pipeline_stages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extract_error: Exception | None = None,
    fail_capability_derivation: bool = False,
) -> RealPipelineFixture:
    """Wire the REAL four pipeline stage functions, running end to end against
    a fake, stateful `_MiniGraph` triple -- replacing `Fake*Stage` wholesale
    substitution (VERIFY_B.md's flagged gap). Only genuine infra/external-call
    boundaries are faked: the three FalkorDB graphs (`_MiniGraph`), the
    Ingestion/Domain-Mapping adapters' own fetch/read methods (a real HTTP
    call and a real native-graph parse respectively -- out of scope for a
    fast unit test, mirrored by every other fake adapter in this test suite),
    and the LLM completion/embedding callers (`extract_roles_and_requirements`/
    `derive_obligations_and_capabilities`'s own `call_completion` parameter --
    a real, first-class DI seam these functions already expose for exactly
    this purpose, not a monkeypatch of their internals). `merge_baseline_graph`
    needs no such wrapper: its own `call_embedding` parameter is bound the
    same way, directly.
    """
    stage_order: list[str] = []
    native = _MiniGraph()
    baseline = _MiniGraph()
    single_tenant = _MiniGraph()
    fake_call_completion = _fake_call_completion(
        fail_capability_derivation=fail_capability_derivation
    )

    def _extract(
        regulatory_instrument_id: str,
        *,
        adapter: object,
        native_graph: object,
        baseline_graph: object,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: object = None,
    ) -> ExtractionResult:
        _ = call_completion
        return extract_roles_and_requirements(
            regulatory_instrument_id,
            adapter=adapter,  # pyright: ignore[reportArgumentType] -- structural DomainMappingAdapter
            native_graph=native_graph,  # pyright: ignore[reportArgumentType] -- structural GraphHandle
            baseline_graph=baseline_graph,  # pyright: ignore[reportArgumentType] -- structural GraphHandle
            model=model,
            call_completion=fake_call_completion,
            emitter=emitter,  # pyright: ignore[reportArgumentType]
        )

    def _derive(
        regulatory_instrument_id: str,
        *,
        baseline_graph: object,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: object = None,
    ) -> DerivationResult:
        _ = call_completion
        return derive_obligations_and_capabilities(
            regulatory_instrument_id,
            baseline_graph=baseline_graph,  # pyright: ignore[reportArgumentType] -- structural GraphHandle
            model=model,
            call_completion=fake_call_completion,
            emitter=emitter,  # pyright: ignore[reportArgumentType]
        )

    def _merge(
        regulatory_instrument_id: str,
        *,
        baseline_graph: object,
        single_tenant_graph: object,
        embed_model: str,
        similarity_threshold: float | None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: object = None,
    ) -> MergeResult:
        _ = call_embedding
        return merge_baseline_graph(
            regulatory_instrument_id,
            baseline_graph=baseline_graph,  # pyright: ignore[reportArgumentType] -- structural GraphHandle
            single_tenant_graph=single_tenant_graph,  # pyright: ignore[reportArgumentType]
            embed_model=embed_model,
            similarity_threshold=similarity_threshold,
            call_embedding=_fake_call_embedding,
            emitter=emitter,  # pyright: ignore[reportArgumentType]
        )

    dependencies = PipelineDependencies(
        graphs=_mini_graph_openers(native, baseline, single_tenant),
        stages=PipelineStages(
            ingest=ingest_regulatory_instrument,
            extract=_extract,
            derive=_derive,
            merge=_merge,
            ingest_internal=_never_ingest_internal,  # pyright: ignore[reportArgumentType]
        ),
        adapters=PipelineAdapters(
            ingestion=lambda: _RecordingIngestionAdapter(stage_order),
            mapping=lambda: _RecordingDomainMappingAdapter(stage_order, error=extract_error),
            internal_seed=_never_internal_seed_adapter,  # pyright: ignore[reportArgumentType]
        ),
    )
    # detroit-exception: this substitutes only `build_default_pipeline_dependencies`
    # itself -- a DI-wiring factory, not a business-logic collaborator -- with a
    # `PipelineDependencies` whose `stages` above are the REAL `ingest_regulatory_
    # instrument`/`extract_roles_and_requirements`/`derive_obligations_and_
    # capabilities`/`merge_baseline_graph` functions (verified above: imported and
    # called directly, never a canned double), and whose `graphs`/`adapters` above
    # substitute only genuine infra/external-content boundaries -- a fake
    # FalkorDB-shaped graph (`_MiniGraph`, the same category `build_default_graph_
    # openers` already covers as an approved boundary) and a fake HTTP-fetch/
    # native-graph-read adapter (the same category every `Fake*Adapter` in this
    # suite already substitutes) -- plus the LLM completion/embedding callers'
    # own first-class `call_completion`/`call_embedding` DI parameters. Slice C's
    # narrowed factory (issue #163) has no `adapters`/`call_completion` parameter
    # of its own to substitute those two pieces individually -- only `graphs` --
    # so patching the factory's return value itself is, unlike Slice C's original
    # violation this replaces, the only seam available to keep those two genuine
    # boundaries fake while every stage's own business logic runs for real. See
    # `.orchestrator/tracker/issue-163/IMPL_SLICE_20.md`.
    monkeypatch.setattr(mcp_server, "build_default_pipeline_dependencies", lambda: dependencies)
    return RealPipelineFixture(
        stage_order=stage_order, native=native, baseline=baseline, single_tenant=single_tenant
    )


# --- Slice 1.4 auth infra: mirrors test_mcp_auth.py's own pattern exactly --


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

    Mirrors `test_mcp_auth.py::_authenticated_test_client` exactly -- this
    module's Slice 1.4 tests drive `ingest_regulation` (not `cypher`) through
    the same genuinely auth-enforcing mounted app, never the bypass-shaped
    one every other test in this module builds via a bare
    `server.call_tool`.
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


def _call_ingest_regulation_over_http(
    client: TestClient, *, token: str, celex: str, short_name: str
) -> str:
    """Drive `ingest_regulation` through the real mounted transport with a
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
                "clientInfo": {"name": "test-ingest-regulation-auth-client", "version": "0.0.1"},
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
                "name": "ingest_regulation",
                "arguments": {"celex": celex, "short_name": short_name},
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


def test_curated_celex_happy_path_returns_ingestion_accepted_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-RESPONSE-SHAPE: the returned dict matches `IngestionAcceptedResponse`
    field for field -- run_id, regulatory_instrument_id, source, outcome, stages.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["run_id"]
    assert isinstance(body["run_id"], str)
    assert body["regulatory_instrument_id"] == _RID
    assert body["source"] == "catalog"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert all(stage["status"] == "succeeded" for stage in body["stages"])
    assert body["outcome"] == "fresh"
    assert set(body.keys()) == {
        "run_id",
        "regulatory_instrument_id",
        "source",
        "outcome",
        "stages",
    }
    for stage in body["stages"]:
        assert set(stage.keys()) == {"stage", "status", "summary"}


def test_curated_celex_happy_path_reports_each_completed_stage_with_a_nonempty_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #163 Slice C rewrite (and Slice 20's real-stage follow-up): the
    tool's own response -- the caller-observable *resulting state*, not an
    assertion on an internal stage-call recorder's order/kwargs (the
    interaction-assertion violation `mcp_interface_part1.md` line 18 flagged)
    -- shows all four REAL stages completed successfully, each reporting a
    real, non-placeholder `summary` derived from its own real computation
    (D-DELEGATE: real orchestration sequencing, proven by its output, not a
    reimplementation). `fail_capability_derivation=True` makes the real
    capability-derivation LLM call return unparseable JSON for this
    fixture's one Obligation, so `derive_obligations_and_capabilities`'s own
    `_mark_capability_unmatched` genuinely records it as unmatched -- this
    test's own proof the real `_execute_catalog_stages` sequencing reached
    the derivation stage's own result and folded it into the response, not a
    canned, order-independent placeholder.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch, fail_capability_derivation=True)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["regulatory_instrument_id"] == _RID
    stages_by_name = {stage["stage"]: stage for stage in body["stages"]}
    assert list(stages_by_name) == ["ingestion", "extraction", "derivation", "merge"]
    assert all(stage["status"] == "succeeded" for stage in stages_by_name.values())
    assert stages_by_name["derivation"]["summary"]["unmatched_obligations"] == 1


def test_curated_celex_happy_path_emits_one_started_succeeded_log_pair_with_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER: `_run_mcp_action` emits exactly one `mcp_interface`
    started/succeeded log-line pair, carrying the resolved principal (here,
    the local-test-bypass principal, since no bearer token is presented to
    a bare `server.call_tool` in-process invocation).
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in lines)
    assert len({line["run_id"] for line in lines}) == 1
    for line in lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def test_curated_celex_mismatched_short_name_is_rejected_before_the_pipeline_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SHORTNAME-CURATED-MISMATCH: a caller-supplied `short_name` that does
    not match the curated entry's own value is rejected with the named error
    string, and no stage ever runs.

    Issue #145: this test predates the tool's authz gate and never set the
    local-test bypass (harmless before the gate existed, since nothing else
    in the un-gated body needed it) -- now required, like every sibling
    body-logic test in this file, so the call reaches D-SHORTNAME-CURATED-MISMATCH
    at all rather than being denied first by the new gate.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    result = _call_ingest_regulation(_CELEX, "not-the-real-short-name")

    assert result.is_error is False
    assert _text(result) == (
        f"error: CELEX {_CELEX} is curated under short_name '{_SHORT_NAME}'; "
        "pass that value, not 'not-the-real-short-name'"
    )
    assert fake.recorder.calls == []


def test_short_name_collision_with_an_already_ingested_different_celex_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-006: a non-curated CELEX whose ``short_name`` is already
    recorded in the single-tenant graph under a *different* CELEX is rejected with
    the named error string, before any pipeline stage runs -- MCP parity with
    ``POST /ingestions``'s own graph-side collision check.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    given_short_name = "eu-1111-2020"
    fake = build_fake_pipeline_dependencies(collision_row=(f"{given_short_name}-1.0", "32024R0001"))
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    result = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)

    assert result.is_error is False
    assert _text(result) == (
        f"error: short_name '{given_short_name}' is already claimed by CELEX 32024R0001"
    )
    assert fake.recorder.calls == []


def test_short_name_collision_with_a_curated_catalog_entry_is_rejected() -> None:
    """Issue #146 AC-BI-006: a curated CELEX whose own ``short_name`` is already
    claimed by a *different* curated entry is rejected with the named error string,
    before any pipeline stage runs -- the catalog-side collision check.

    Issue #163 Slice C / AUDIT.md §2 case 11: the real curated catalog can
    never itself produce this collision (no two curated entries share a
    ``short_name``, by construction), so exercising this branch needs a
    fixture catalog -- previously injected by monkeypatching the module-global
    ``ps_service.api.catalog.REGULATION_CATALOG`` constant (a DI-gap smell
    per the ruling, not a legitimate mock-boundary substitution). Now that
    ``validate_and_resolve_catalog_entry`` takes a real ``catalog`` parameter,
    this test calls it directly with the fixture -- the same real function
    the MCP tool's own (unpatched) `_resolve_and_ingest` calls, exercised at
    its own natural unit boundary rather than through a full tool round-trip
    that has no client-facing seam for this fixture at all.
    """
    fixture = (
        CatalogEntry("32024R0001", "Fixture One", "shared-name", "1.0"),
        CatalogEntry("32024R0002", "Fixture Two", "shared-name", "1.0"),
    )

    with pytest.raises(ShortNameCollisionError) as exc_info:
        validate_and_resolve_catalog_entry(
            "32024R0001",
            "shared-name",
            single_tenant_graph=FakeGraphHandle(),
            catalog=fixture,
        )

    assert str(exc_info.value) == (
        "short_name 'shared-name' is already claimed by CELEX 32024R0002"
    )


def test_non_curated_celex_repeated_ingestion_with_drifting_titles_resolves_identical_short_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #96 (CHANGES.md C1/Appendix C1).

    Two sequential `ingest_regulation` calls for the same non-curated CELEX,
    against a fake Cellar/ELI adapter that returns a *differently-worded*
    title on each call (simulating a re-crawl rendering drift), must both
    resolve to the identical, caller-supplied `short_name` -- the same value
    used to construct the `{short_name}_native`/`{short_name}_baseline` graph
    keys (`ingestion/falkordb_client.py`). Before this fix, `resolve_via_cellar`
    derived `short_name` by slugging the fetched title internally, so these
    two calls would have diverged into two different graph names for the
    same CELEX -- the exact "forks a duplicate RegulatoryInstrument subtree"
    defect #96 describes. Now, `_derive_short_name` is never reached on this
    path at all.

    Issue #163 Slice 20: both calls now run the REAL ingest stage --
    `resolve_via_cellar`'s own real `CellarEliAdapter`, bound to each call's
    own cached xhtml/rdf bytes, is never itself faked (it wasn't before
    either: the pre-Slice-20 `FakeIngestStage` never called its own
    `adapter` argument at all, so this real adapter's parse was already
    exercised-but-unasserted). Since both calls resolve to the identical
    `regulatory_instrument_id` (the actual #96 regression proof), the
    SECOND call's real pre-flight check (`_is_already_merged`, issue #135)
    now genuinely finds the first call's real merge already persisted in
    the shared single-tenant graph and short-circuits -- a stronger, real
    proof of convergence than the old recorder-based per-call kwarg check,
    which only ever inspected a hand-written fake's own call log.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch)

    titles = iter(
        [
            "Regulation (EU) 1111/1111 First Crawl Title",
            "Regulation (EU) 1111/1111 Reworded On A Later Crawl",
        ]
    )

    def _fetch_xhtml(celex: str) -> bytes:
        _ = celex
        return _xhtml_fixture(next(titles))

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch_xhtml)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)

    given_short_name = "eu-1111-2020"
    first = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)
    second = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)

    assert first.is_error is False
    assert second.is_error is False
    first_body = json.loads(_text(first))
    second_body = json.loads(_text(second))
    assert first_body["regulatory_instrument_id"] == second_body["regulatory_instrument_id"]
    assert first_body["outcome"] == "fresh"
    assert second_body["outcome"] == "already_ingested"


def test_non_curated_celex_not_found_on_cellar_returns_catalog_identifier_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: a CELEX absent from both the curated catalog and
    Cellar/ELI returns `error: <str(exc)>` verbatim, and no stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies()
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)

    result = _call_ingest_regulation(_NONCURATED_CELEX, "some-short-name")

    assert result.is_error is False
    expected = (
        f"No curated regulation has CELEX {_NONCURATED_CELEX!r}, "
        "and it does not exist on Cellar/ELI."
    )
    assert _text(result) == f"error: {expected}"
    assert fake.recorder.calls == []


# --- Slice 1.3: remaining D-SANITIZE-UNEXPECTED rows for this tool ----------


def test_ingestion_config_incomplete_returns_named_error_before_any_stage_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `IngestionConfigIncompleteError` (raised by
    `_require_ingestion_config`, before any graph or stage call) surfaces as
    `error: <str(exc)>` verbatim, and no stage ever runs.
    """
    monkeypatch.delenv("PS_LLMINTERFACE_MODEL", raising=False)
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == (
        "error: ingestion configuration incomplete: llm_interface_model, "
        "llm_interface_embed_model, company_merge_similarity_threshold not set"
    )
    assert fake.recorder.calls == []


def test_pipeline_stage_error_surfaces_verbatim_with_sanitised_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: a genuine mid-pipeline stage failure surfaces as
    `error: <str(exc)>` verbatim -- already sanitised at the source
    (`_classify_stage_failure`): a non-`is_safe_verbatim`-whitelisted
    exception (a bare `RuntimeError`, not a whitelisted domain error) never
    leaks its own message, only the generic `"<stage> failed"` reason.
    Later stages never run.

    Issue #163 Slice 20: the failure is now injected at the real extraction
    stage's own real, external-content boundary -- the Domain Mapping
    Adapter's `read_native_units` call, the first thing
    `extract_roles_and_requirements` does -- rather than a wholesale fake
    stage substitution; the real stage function genuinely raises this
    exception and genuinely propagates it through the real
    `_run_stage`/`_classify_stage_failure` machinery under test.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fixture = _use_real_pipeline_stages(
        monkeypatch, extract_error=RuntimeError("boom -- must never reach the caller")
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: extraction stage failed: extraction failed"
    assert fixture.stage_order == ["ingestion", "extraction"]
    # The real ingest stage genuinely completed (RegulatoryInstrument
    # registered in the native graph) before extraction genuinely failed --
    # derive/merge never touched the baseline graph at all.
    assert fixture.native.has_node("RegulatoryInstrument", _RID)
    assert fixture.baseline.is_empty()


def test_llm_interface_unhealthy_returns_named_error_without_opening_any_graph(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-PREFLIGHT: when `dependency_health.is_healthy(LLM_INTERFACE)` is
    `False`, the tool fails fast with the exact `handlers.py:72` message,
    before any graph is opened or the pipeline is called -- and the failure
    is still logged with the resolved principal.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    # No pipeline-dependencies patch needed: the D-PREFLIGHT LLM-health check
    # below runs before `_resolve_and_ingest`/`build_default_pipeline_dependencies`
    # is ever reached, so the real, unpatched factory is simply never called.
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: LLM Interface is unavailable."
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in lines)


def test_graph_unavailable_returns_named_error_when_native_graph_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_catalog_ingestion_pipeline` opens all three
    graphs before `_execute_catalog_stages`/`_run_stage` ever runs, with no
    try/except of its own -- a generic exception from any one of the three
    openers must sanitise to the same fixed message `_resolve_graph` already
    uses for `cypher`, never leaking host/port/driver detail, and no stage
    ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_native(config: object, short_name: str) -> object:
        _ = (config, short_name)
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_graphs = dataclasses.replace(fake.dependencies.graphs, native=_raising_native)
    _use_fake_graph_openers_only(monkeypatch, broken_graphs)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert fake.recorder.calls == []


def test_collision_check_graph_unreachable_returns_graph_unavailable_error_without_opening_any_pipeline_graph(  # noqa: E501 - name mirrors the sibling graph-unavailable test's verbatim-scenario naming
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-007: `_resolve_and_ingest` opens the single-tenant
    graph itself (to run the collision check) before
    `run_catalog_ingestion_pipeline` ever runs -- that open is wrapped by the
    same `_sanitize_pipeline_graph_opens`/`_sanitize_graph_open` machinery
    `test_graph_unavailable_returns_named_error_when_native_graph_open_fails`
    already proves for the pipeline's own three graph opens, so a failure
    opening the single-tenant graph here must sanitise to the exact same
    fixed, generic message -- never leaking driver/host detail -- and no
    pipeline stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_single_tenant(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_graphs = dataclasses.replace(
        fake.dependencies.graphs, single_tenant=_raising_single_tenant
    )
    _use_fake_graph_openers_only(monkeypatch, broken_graphs)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert fake.recorder.calls == []


def test_collision_check_query_failure_surfaces_as_pipeline_stage_error_not_graph_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-007: when the single-tenant graph *opens* successfully
    but the collision-check *query itself* fails, that failure never touches
    `_sanitize_graph_open` (which only wraps the open) -- it is caught by
    `check_short_name_collision`'s own `_run_stage` wrapping inside
    `validate_and_resolve_catalog_entry`, becomes a `PipelineStageError`, and
    is caught by `_resolve_and_ingest`'s existing
    `except (..., PipelineStageError)` clause, surfacing as
    `f"error: {exc}"` with the real stage-failure text -- distinct from the
    generic `_GRAPH_UNAVAILABLE_MESSAGE` the graph-*open* failure mode
    produces in the sibling test above. No pipeline stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(
        rid="cra-1.0", collision_error=RuntimeError("boom -- must never reach the caller")
    )
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: collision_check stage failed: collision_check failed"
    assert fake.recorder.calls == []


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `ingest_regulation`'s own body does not itself sanitise (here,
    `routes._to_accepted_response` blowing up after a fully successful
    pipeline run) is caught by `_run_mcp_action`'s residual safety net --
    returned as the fixed, generic message (never the raw exception text),
    with the full `repr` logged server-side only.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    def _boom(run_id: str, outcome: object) -> object:
        _ = (run_id, outcome)
        raise RuntimeError("boom -- must never reach the caller")

    # AUDIT.md §2 case 13: `_to_accepted_response` is a pure, always-succeeding
    # response-shaping function for any well-formed `IngestionOutcome`; there is
    # no real input that makes it fail, so proving this residual safety net
    # (D-AUDIT-WRAPPER point 4) genuinely requires forcing this otherwise-
    # unreachable defensive branch.
    # detroit-exception: legitimate fault-injection carve-out, see comment above.
    monkeypatch.setattr(mcp_server, "_to_accepted_response", _boom)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "RuntimeError" in str(failed_line.get("detail"))


# --- Slice 1.4: auth/audit completeness under a real verified bearer token --


def _grant_compliance_officer(
    monkeypatch: pytest.MonkeyPatch, *, subject: str, issuer: str
) -> None:
    """Issue #145: seed a `ComplianceOfficer` grant for `(subject, issuer)` on a fake store,
    monkeypatched onto `mcp_server.PsycopgAccessRoleStore`.

    The two real-verified-token tests below predate issue #145's authz gate
    (they were written to prove principal/caller threading, not
    authorization) -- without this, the newly-added gate now denies them
    (or, in the LLM-unhealthy case, denies with the wrong error string)
    since the token's own subject holds no grant on the real store, which
    itself is unreachable in this test environment (`PS_AUTHZ_POSTGRES_HOST`
    unset) and would otherwise fail closed. Mirrors
    `test_catalog_source_authz_gate.py`'s own `_fake_store_factory` pattern.
    """
    store = FakeAccessRoleStore()
    store.grant(
        actor=(subject, issuer), target=(subject, issuer), access_role=AccessRole.COMPLIANCE_OFFICER
    )

    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


def test_real_verified_token_principal_threads_through_to_both_audit_log_and_pipeline_caller(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """D-AUTH (a): under a real, verified bearer token -- driven through the
    actual mounted Streamable HTTP transport, mirroring `test_mcp_auth.py`'s
    own pattern, never a bare in-process `server.call_tool` -- the token's
    `sub` claim (never `LOCAL_TEST_PRINCIPAL_ID`) reaches both
    `_run_mcp_action`'s `mcp_interface` log entries (`principal`) and
    `run_catalog_ingestion_pipeline`'s own `ingestion_run` log entries
    (`caller`). Both were already wired from Slice 1.1's `principal =
    _resolve_principal(config)` / `caller=principal or "unknown"` plumbing --
    this is the first test to prove it under a real verified token rather
    than the local-test-bypass principal every other test in this module
    exercises.

    Issue #145: the caller must also hold `ComplianceOfficer` now that this
    tool is gated, so `_grant_compliance_officer` seeds that grant for this
    token's own `(sub, iss)` -- orthogonal to what this test itself proves.
    """
    _configure_complete_llm_env(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-ingest-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    _use_real_pipeline_stages(monkeypatch)

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_ingest_regulation_over_http(
            client, token=token, celex=_CELEX, short_name=_SHORT_NAME
        )

    body = json.loads(result_text)
    assert body["regulatory_instrument_id"] == _RID

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())

    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    assert not any(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)

    run_lines = [
        line
        for line in all_lines
        if line.get("component") == "api" and line.get("action") == "ingestion_run"
    ]
    assert len(run_lines) == 2
    assert all(line.get("caller") == token_sub for line in run_lines)


def test_local_test_bypass_principal_still_threads_through_when_no_token_is_presented(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUTH (b): `_resolve_principal`'s existing, unchanged fallback
    contract -- with no bearer token presented at all (a bare in-process
    `server.call_tool`, like every other test above) and the local-test
    bypass active, `LOCAL_TEST_PRINCIPAL_ID` still reaches both the
    `mcp_interface` log entries' `principal` (already proven by Slice 1.1's
    own test) and, newly proven here, `run_catalog_ingestion_pipeline`'s own
    `ingestion_run` log entries' `caller`.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    _use_real_pipeline_stages(monkeypatch)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())

    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)

    run_lines = [
        line
        for line in all_lines
        if line.get("component") == "api" and line.get("action") == "ingestion_run"
    ]
    assert len(run_lines) == 2
    assert all(line.get("caller") == LOCAL_TEST_PRINCIPAL_ID for line in run_lines)


def test_failed_call_under_a_real_verified_token_still_carries_the_subs_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """D-AUTH (c): a FAILED call -- reusing Slice 1.3's LLM-unhealthy
    D-PREFLIGHT branch -- still carries the resolved real-token principal on
    its `outcome="failed"` `mcp_interface` log entry, not only the
    succeeded-call case proven above. No `ingestion_run` entry is emitted at
    all here, since D-PREFLIGHT fails before the pipeline is ever called.

    Issue #145: the caller must hold `ComplianceOfficer` for this call to
    reach the D-PREFLIGHT branch at all -- see `_grant_compliance_officer`.
    """
    _configure_complete_llm_env(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-ingest-failed-7"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    # No pipeline-dependencies patch needed here either -- same reasoning as
    # `test_llm_interface_unhealthy_returns_named_error_without_opening_any_graph`:
    # D-PREFLIGHT fails before the real, unpatched factory is ever called.
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_ingest_regulation_over_http(
            client, token=token, celex=_CELEX, short_name=_SHORT_NAME
        )

    assert result_text == "error: LLM Interface is unavailable."
    assert fake.recorder.calls == []

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "failed"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    run_lines = [line for line in all_lines if line.get("action") == "ingestion_run"]
    assert run_lines == []
