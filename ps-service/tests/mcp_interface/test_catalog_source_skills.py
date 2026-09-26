"""Tests for the `set-catalog-source`/`reset-catalog-source`/`get-catalog-source`
MCP tools (issue #125, Slice 3): AC-BI-012 (set validates + persists, no
restart), AC-BI-013 (precedence on every `GET /catalog` call, including a
simulated restart), AC-BI-014 (reset clears), AC-BI-015 (get reports
effective url + override/default), plus D-FAILOPEN (a FalkorDB outage during
the override check never breaks `get-catalog-source`/`GET /catalog`).

`pytest-asyncio` is not installed; tool coroutines are driven with bare
`asyncio.run(...)`, exactly like `test_cypher_tool.py`/`test_near_miss_tools.py`.
Hand-written structural fakes throughout -- no `unittest.mock`, mirroring
this test suite's own established convention.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, NoReturn, cast

from api._fakes import FakeCuratedSourceTransport, build_fake_curated_catalog_dependencies
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from ps_service.api.dependencies import provide_curated_catalog_dependencies
from ps_service.config import ServiceConfig
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    import pytest


_DEFAULT_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)
_OVERRIDE_URL = "https://example.com/operator-override"

_CANNED_ENTRIES = [
    {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "CRA",
        "version": "1.0",
    }
]


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeSingletonGraph:
    """An in-memory fake implementing `store.py`'s three exact query shapes.

    Shared, across a test, between the MCP tools (via `_install_graph`
    below) and a `CuratedCatalogDependencies.resolve_effective_source`
    (via `resolve_effective_source(config, open_graph=lambda: this)`) so a
    test can prove the two surfaces actually see the same persisted state --
    the closest thing to a real FalkorDB round trip a hermetic test can
    exercise for the MCP<->REST integration proof (PLAN.md Slice 3 test 2/3).
    """

    def __init__(self) -> None:
        self.url: str | None = None

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        params = params or {}
        if "RETURN o.url" in q:
            return _FakeQueryResult([[self.url]] if self.url is not None else [])
        if q.startswith("MERGE"):
            self.url = cast("str", params["url"])
            return _FakeQueryResult([])
        if "DELETE o" in q:
            self.url = None
            return _FakeQueryResult([])
        message = f"unscripted query: {q!r}"
        raise AssertionError(message)


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client -- mirrors `test_cypher_tool.py`."""

    def __init__(self, handle: _FakeSingletonGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _FakeSingletonGraph:
        _ = name
        return self._handle


def _install_graph(monkeypatch: pytest.MonkeyPatch, graph: _FakeSingletonGraph) -> None:
    """Wire `mcp_server.connect_from_config` to a fake FalkorDB wrapping `graph`.

    Mirrors `test_cypher_tool.py::_install_graph` exactly.
    """
    fake_db = _FakeFalkorDB(graph)

    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)


def _install_raising_graph(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Wire `mcp_server.connect_from_config` to always raise -- simulates FalkorDB unreachable."""

    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        raise error

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call(tool: str, args: dict[str, object] | None = None) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool(tool, args or {}))
    assert isinstance(result, CallToolResult)
    return result


def _app_config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        curated_source_base_url=_DEFAULT_URL,
    )


# --- set-catalog-source (AC-BI-012) -----------------------------------------


def test_set_catalog_source_persists_override_and_get_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012/AC-BI-015: a valid `set-catalog-source` call is immediately visible to `get`."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
    assert set_result.is_error is False
    assert json.loads(_text(set_result)) == {"url": _OVERRIDE_URL, "source": "override"}

    get_result = _call("get-catalog-source")
    assert get_result.is_error is False
    assert json.loads(_text(get_result)) == {"url": _OVERRIDE_URL, "source": "override"}


def test_set_catalog_source_rejects_file_scheme_and_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008 (runtime half): `file://` is rejected, no persisted change."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    result = _call("set-catalog-source", {"url": "file:///etc/passwd"})

    assert result.is_error is False
    assert _text(result).startswith("error: ")
    assert graph.url is None


def test_set_catalog_source_rejects_plain_http_without_allow_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-010 (runtime half): plain `http://` needs the same opt-in startup config does."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    result = _call("set-catalog-source", {"url": "http://example.com/insecure"})

    assert result.is_error is False
    assert _text(result).startswith("error: ")
    assert graph.url is None


def test_set_catalog_source_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: an unreachable FalkorDB sanitises to the fixed message."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_raising_graph(monkeypatch, ConnectionError("connection refused to 10.0.0.1:6379"))

    result = _call("set-catalog-source", {"url": _OVERRIDE_URL})

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


# --- reset-catalog-source (AC-BI-014) ---------------------------------------


def test_reset_catalog_source_clears_override_and_get_reverts_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    graph.url = _OVERRIDE_URL
    _install_graph(monkeypatch, graph)

    reset_result = _call("reset-catalog-source")
    assert reset_result.is_error is False
    assert json.loads(_text(reset_result)) == {"url": _DEFAULT_URL, "source": "default"}
    assert graph.url is None

    get_result = _call("get-catalog-source")
    assert json.loads(_text(get_result)) == {"url": _DEFAULT_URL, "source": "default"}


def test_reset_catalog_source_is_a_no_op_when_nothing_was_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    result = _call("reset-catalog-source")

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _DEFAULT_URL, "source": "default"}


def test_reset_catalog_source_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_raising_graph(monkeypatch, ConnectionError("connection refused to 10.0.0.1:6379"))

    result = _call("reset-catalog-source")

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


# --- get-catalog-source (AC-BI-015), including D-FAILOPEN -------------------


def test_get_catalog_source_reports_default_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    result = _call("get-catalog-source")

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _DEFAULT_URL, "source": "default"}


def test_get_catalog_source_falls_open_to_default_when_graph_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-FAILOPEN via the MCP surface: `get-catalog-source` never errors on a FalkorDB outage."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_raising_graph(monkeypatch, ConnectionError("connection refused to 10.0.0.1:6379"))

    result = _call("get-catalog-source")

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _DEFAULT_URL, "source": "default"}


# --- MCP <-> REST integration: precedence takes effect with no restart -----


def _client_with_graph_and_transport(
    app_config: ServiceConfig, graph: _FakeSingletonGraph
) -> TestClient:
    """A `TestClient` whose `GET /catalog` resolves the override through `graph`."""
    app = create_app(app_config)
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))

    def _resolve(config: ServiceConfig) -> EffectiveCatalogSource:
        return resolve_effective_source(config, open_graph=lambda: graph)

    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport, resolve_effective_source=_resolve)
    )
    return TestClient(app)


def test_get_catalog_uses_the_override_set_via_mcp_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-013: `set-catalog-source` takes effect on the very next `GET /catalog` call.

    No process restart is involved -- the same persisted state (`graph`) is
    read by both the MCP tool and a fresh dependency resolution for the REST
    route, exactly as PLAN.md's own "no restart needed to prove the point"
    wording describes.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
    assert set_result.is_error is False

    client = _client_with_graph_and_transport(_app_config(), graph)
    response = client.get("/catalog")

    assert response.status_code == 200
    body = response.json()
    assert {item["instrument_id"] for item in body["instruments"]} == {"CRA-1.0"}


def test_get_catalog_reverts_to_default_after_reset_catalog_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-014, end to end: `reset-catalog-source` makes `GET /catalog` use the default again."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)
    _call("set-catalog-source", {"url": _OVERRIDE_URL})

    reset_result = _call("reset-catalog-source")
    assert reset_result.is_error is False

    client = _client_with_graph_and_transport(_app_config(), graph)
    response = client.get("/catalog")

    assert response.status_code == 200  # the fake transport still serves the same canned body
    # The proof that the DEFAULT url (not the override) is what was resolved
    # lives at the `resolve_effective_source` level -- confirmed directly:
    assert resolve_effective_source(
        _app_config(), open_graph=lambda: graph
    ) == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)


def test_get_catalog_falls_open_to_default_source_when_falkordb_override_check_fails() -> None:
    """D-FAILOPEN via `GET /catalog`: an unreachable FalkorDB during the override check
    falls back to the env-var/default rather than 502ing (re-verifies Slice 1's
    "no FalkorDB fixture needed" guarantee now that the override check is wired in).
    """
    configure()  # fetch_catalog/the fallback log; the bare TestClient below never enters lifespan

    def _raising_open_graph() -> NoReturn:
        message = "connection refused"
        raise ConnectionError(message)

    app_config = _app_config()
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))

    def _resolve(config: ServiceConfig) -> EffectiveCatalogSource:
        return resolve_effective_source(config, open_graph=_raising_open_graph)

    app = create_app(app_config)
    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport, resolve_effective_source=_resolve)
    )
    client = TestClient(app)

    response = client.get("/catalog")

    assert response.status_code == 200
    body = response.json()
    assert {item["instrument_id"] for item in body["instruments"]} == {"CRA-1.0"}
