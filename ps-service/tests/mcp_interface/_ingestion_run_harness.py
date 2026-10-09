"""Shared helpers for the `start_ingestion` / `get_ingestion_status` tool tests (issue #194).

Follows the `tests/passkey_signing/_harness.py` precedent: one module the tool-level test files
import from, so the in-memory store wiring, the in-process tool calls and the per-test isolation
of the process-wide dispatch and live-stage registries are declared once.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from typing import TYPE_CHECKING

from ingestion_runs._fakes import InMemoryIngestionRunStore
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_ingest_regulation_tool import (
    _MiniGraph,  # pyright: ignore[reportPrivateUsage]  -- the stateful in-memory graph double, reused for the per-short_name graphs
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- reuse the real-stage fixture verbatim, mirrors `test_ingest_regulation_authz_gate.py`
)
from ps_service.api import run_status
from ps_service.api.ingestion_orchestration import GraphOpeners
from ps_service.ingestion_runs import dispatch
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from ps_service.domain_mapper.models import ExtractionUnit

_GATE_WAIT_SECONDS = 10.0


def install_ingestion_run_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryIngestionRunStore:
    """Replace the tools' `PsycopgIngestionRunStore` with one shared in-memory store.

    The factory accepts (and ignores) `config` and any keyword, matching how the tool bodies
    construct the real store (`test_ingest_regulation_authz_gate.py`'s `_fake_store_factory`).
    """
    store = InMemoryIngestionRunStore()

    def _factory(_config: object, **_kwargs: object) -> InMemoryIngestionRunStore:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgIngestionRunStore", _factory)
    return store


def call_tool(tool: str, arguments: dict[str, str]) -> CallToolResult:
    """Call an MCP tool in-process; a schema rejection raises `ToolError`."""
    result = asyncio.run(mcp_server.server.call_tool(tool, arguments))
    assert isinstance(result, CallToolResult)
    return result


def call_start_ingestion(celex: str, short_name: str) -> CallToolResult:
    """Call `start_ingestion` in-process."""
    return call_tool("start_ingestion", {"celex": celex, "short_name": short_name})


def call_get_ingestion_status(run_id: str) -> CallToolResult:
    """Call `get_ingestion_status` in-process."""
    return call_tool("get_ingestion_status", {"run_id": run_id})


def text(result: CallToolResult) -> str:
    """The single text block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def body(result: CallToolResult) -> dict[str, object]:
    """The JSON object a tool returned (fails if the tool returned an `error:` string)."""
    parsed = json.loads(text(result))
    assert isinstance(parsed, dict)
    return {str(key): value for key, value in parsed.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def wait_for_run(run_id: str) -> None:
    """Block until the run's background worker has finished and released its slot."""
    dispatch.wait_for_tests(run_id, timeout_seconds=10)


def isolate_ingestion_runs() -> None:
    """Reset the in-flight registry and the live-stage registry (call before and after a test)."""
    dispatch.reset_for_tests()
    run_status.reset_for_tests()


class GatedMappingAdapter:
    """The real-stage fixture's mapping adapter, held at `read_native_units` until its gate opens.

    `read_native_units` is the first thing the extraction stage does after the live-stage
    registry has been set to `extraction`, so a closed gate parks the worker inside that stage.
    `gate_for` picks the gate from the native graph being read, so runs on different graphs
    can be released independently. The wait is bounded so a broken test cannot hang the suite.
    """

    def __init__(self, inner: object, gate_for: Callable[[object], threading.Event]) -> None:
        self._inner = inner
        self._gate_for = gate_for

    def read_native_units(
        self, graph: object, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        self._gate_for(graph).wait(timeout=_GATE_WAIT_SECONDS)
        return self._inner.read_native_units(graph, regulatory_instrument_id)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]


@dataclasses.dataclass(frozen=True, slots=True)
class GatedPipeline:
    """Handle on the gated pipeline.

    `gate` releases every run when graphs are shared. With `per_short_name_graphs=True` each
    `short_name` has its own native graph and its own gate, opened with `release(short_name)`.
    """

    gate: threading.Event
    short_name_gates: dict[str, threading.Event]

    def release(self, short_name: str) -> None:
        """Let the run for `short_name` proceed (per-short_name mode only)."""
        # Ingestion normalizes `short_name` to upper case (issue #193) before opening graphs.
        self.short_name_gates[short_name.upper()].set()

    def release_all(self) -> None:
        """Let every parked run proceed."""
        self.gate.set()
        for gate in list(self.short_name_gates.values()):
            gate.set()


def use_gated_real_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, per_short_name_graphs: bool = False
) -> GatedPipeline:
    """Wire the real pipeline stages, with the extraction stage parked on a closed gate.

    With `per_short_name_graphs=True` the native and baseline graphs are distinct per
    `short_name` (the single-tenant graph stays shared) and each native graph has its own gate.
    """
    _use_real_pipeline_stages(monkeypatch)
    deps = mcp_server.build_default_pipeline_dependencies()
    gate = threading.Event()
    short_name_gates: dict[str, threading.Event] = {}
    graphs = deps.graphs
    registry_lock = threading.Lock()
    gate_by_graph: dict[int, threading.Event] = {}

    def gate_for(graph: object) -> threading.Event:
        with registry_lock:
            return gate_by_graph.get(id(graph), gate)

    if per_short_name_graphs:
        natives: dict[str, _MiniGraph] = {}
        baselines: dict[str, _MiniGraph] = {}

        def _open_native(_config: object, short_name: str) -> _MiniGraph:
            with registry_lock:
                if short_name not in natives:
                    natives[short_name] = _MiniGraph()
                    short_name_gates[short_name] = threading.Event()
                    gate_by_graph[id(natives[short_name])] = short_name_gates[short_name]
                return natives[short_name]

        def _open_baseline(_config: object, short_name: str) -> _MiniGraph:
            with registry_lock:
                return baselines.setdefault(short_name, _MiniGraph())

        graphs = GraphOpeners(
            native=_open_native, baseline=_open_baseline, single_tenant=deps.graphs.single_tenant
        )

    inner_mapping = deps.adapters.mapping
    gated = dataclasses.replace(
        deps,
        graphs=graphs,
        adapters=dataclasses.replace(
            deps.adapters, mapping=lambda: GatedMappingAdapter(inner_mapping(), gate_for)
        ),
    )
    # detroit-exception: re-patches only `build_default_pipeline_dependencies` (a DI-wiring
    # factory) with the dependencies `_use_real_pipeline_stages` already built, differing only
    # in that the mapping adapter -- the external native-graph-read boundary, the same category
    # that fixture already fakes (see `test_ingest_regulation_tool.py` rationale at its
    # `monkeypatch.setattr(mcp_server, "build_default_pipeline_dependencies", ...)`) -- waits on
    # a gate before delegating, and (per-short_name mode) the graph openers hand out one
    # `_MiniGraph` per short_name, the same fake-FalkorDB category. Every stage's business
    # logic still runs for real.
    monkeypatch.setattr(mcp_server, "build_default_pipeline_dependencies", lambda: gated)
    return GatedPipeline(gate=gate, short_name_gates=short_name_gates)
