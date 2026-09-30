"""Tests for the `set-catalog-source`/`reset-catalog-source`/`get-catalog-source`
MCP tools (issues #125, #130): the tools keep their names and contracts while the override now
lives in `runtime_config` (a hand-written `InMemoryRuntimeConfigStore` stands in for
`PsycopgRuntimeConfigStore` at the persistence boundary; it still applies the catalog key's
real type check and validator). AC-BI-006 (the shared validator rejects), AC-BI-007/008
(set/get/reset round trips and REST parity), AC-BI-010/011 (fail closed / named errors), and
CHANGES F4 (the local-test bypass records a sentinel actor).

`pytest-asyncio` is not installed; tool coroutines are driven with bare
`asyncio.run(...)`, exactly like `test_cypher_tool.py`/`test_near_miss_tools.py`.
Hand-written structural fakes throughout -- no `unittest.mock`.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from api._fakes import FakeCuratedSourceTransport, build_fake_curated_catalog_dependencies
from curated_source._fakes import InMemoryRuntimeConfigStore
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from ps_service.api.dependencies import provide_curated_catalog_dependencies
from ps_service.config import ServiceConfig
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.curated_source.source_url import validate_source_url
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server
from ps_service.runtime_config import RuntimeConfigUnavailableError

_DEFAULT_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)
_OVERRIDE_URL = "https://example.com/operator-override"
_LOCAL_TEST_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")
_STORE_UNAVAILABLE_ERROR = "error: The runtime configuration store is temporarily unavailable."
_WRITE_FAILED_ERROR = "error: The runtime configuration write failed and was not applied."

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


def _install_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryRuntimeConfigStore:
    """Wire `mcp_server.PsycopgRuntimeConfigStore` to one shared in-memory store.

    Each tool body calls `PsycopgRuntimeConfigStore(config, audit_store=...)`, so the factory
    accepts (and ignores) the audit store and hands back the same store bound to the
    `config` that call resolved -- so a test can also inspect or script it afterwards.
    """
    store = InMemoryRuntimeConfigStore(_app_config())

    def _factory(config: ServiceConfig, **_kwargs: object) -> InMemoryRuntimeConfigStore:
        store.config = config
        return store

    monkeypatch.setattr(mcp_server, "PsycopgRuntimeConfigStore", _factory)
    return store


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
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _shared_validator_message(url: str) -> str:
    """What `validate_source_url` itself says about `url` (`set-catalog-source` must echo it)."""
    with pytest.raises(CuratedSourceConfigurationError) as exc_info:
        validate_source_url(url, allow_insecure_http=False)
    return str(exc_info.value)


# --- set-catalog-source -----------------------------------------------------


def test_set_then_get_round_trip_uses_runtime_config_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-007: a valid `set-catalog-source` call is persisted and visible to `get` at once."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)

    set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
    assert set_result.is_error is False
    assert json.loads(_text(set_result)) == {"url": _OVERRIDE_URL, "source": "override"}

    get_result = _call("get-catalog-source")
    assert get_result.is_error is False
    assert json.loads(_text(get_result)) == {"url": _OVERRIDE_URL, "source": "override"}
    assert store.rows == {CATALOG_SOURCE_KEY: _OVERRIDE_URL}


def test_set_catalog_source_rejects_file_scheme_and_plain_http_via_shared_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006: rejected exactly as before -- same validator, same message, nothing written."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)

    for url in ("file:///etc/passwd", "http://example.com/insecure"):
        result = _call("set-catalog-source", {"url": url})

        assert result.is_error is False
        assert _text(result) == f"error: {_shared_validator_message(url)}"
    assert store.rows == {}
    assert store.writes == []


def test_set_catalog_source_under_local_test_bypass_records_sentinel_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES F4: audit rows need an actor, and the bypass has no real one."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)

    _call("set-catalog-source", {"url": _OVERRIDE_URL})

    assert [(write.action, write.actor) for write in store.writes] == [
        ("set", _LOCAL_TEST_BYPASS_ACTOR)
    ]


def test_reset_catalog_source_under_local_test_bypass_records_sentinel_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)

    _call("reset-catalog-source")

    assert [(write.action, write.actor) for write in store.writes] == [
        ("reset", _LOCAL_TEST_BYPASS_ACTOR)
    ]


def test_set_and_reset_return_named_error_string_when_config_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-011: a failed write (or its audit insert) is a named `error:` string, no change."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    store.rows[CATALOG_SOURCE_KEY] = "https://example.com/existing"
    store.fail_writes = True

    set_result = _call("set-catalog-source", {"url": _OVERRIDE_URL})
    reset_result = _call("reset-catalog-source")

    assert _text(set_result) == _WRITE_FAILED_ERROR
    assert _text(reset_result) == _WRITE_FAILED_ERROR
    assert store.rows == {CATALOG_SOURCE_KEY: "https://example.com/existing"}


def test_set_and_reset_return_the_store_unavailable_error_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()

    def _unreachable(_config: object, **_kwargs: object) -> object:
        raise RuntimeConfigUnavailableError(
            "The runtime configuration store is temporarily unavailable."
        )

    monkeypatch.setattr(mcp_server, "PsycopgRuntimeConfigStore", _unreachable)

    assert _text(_call("set-catalog-source", {"url": _OVERRIDE_URL})) == _STORE_UNAVAILABLE_ERROR
    assert _text(_call("reset-catalog-source")) == _STORE_UNAVAILABLE_ERROR


# --- reset-catalog-source ---------------------------------------------------


def test_reset_then_get_returns_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-008."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    store.rows[CATALOG_SOURCE_KEY] = _OVERRIDE_URL

    reset_result = _call("reset-catalog-source")
    assert reset_result.is_error is False
    assert json.loads(_text(reset_result)) == {"url": _DEFAULT_URL, "source": "default"}
    assert store.rows == {}

    get_result = _call("get-catalog-source")
    assert json.loads(_text(get_result)) == {"url": _DEFAULT_URL, "source": "default"}


def test_reset_catalog_source_is_a_no_op_when_nothing_was_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_store(monkeypatch)

    result = _call("reset-catalog-source")

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _DEFAULT_URL, "source": "default"}


# --- get-catalog-source, fail closed ---------------------------------------


def test_get_catalog_source_reports_default_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_store(monkeypatch)

    result = _call("get-catalog-source")

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _DEFAULT_URL, "source": "default"}


def test_get_catalog_source_returns_error_string_when_override_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-010: no fall back to the default source, and no host/port/driver detail."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    store.fail_reads = True

    result = _call("get-catalog-source")

    assert result.is_error is False
    assert _text(result) == _STORE_UNAVAILABLE_ERROR
    assert _DEFAULT_URL not in _text(result)


def test_get_catalog_source_returns_error_string_when_stored_value_no_longer_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    store.rows[CATALOG_SOURCE_KEY] = "file:///etc/passwd"

    result = _call("get-catalog-source")

    assert _text(result) == _STORE_UNAVAILABLE_ERROR


def test_catalog_source_tools_are_exactly_three_and_names_unchanged() -> None:
    """AC-BI-001: no new tool (in particular no generic `set-config`) was added."""
    tools = asyncio.run(mcp_server.server.list_tools())
    names = {tool.name for tool in tools}

    assert {name for name in names if "catalog-source" in name} == {
        "set-catalog-source",
        "get-catalog-source",
        "reset-catalog-source",
    }
    assert [name for name in names if "config" in name] == []


# --- MCP <-> REST integration: precedence takes effect with no restart -----


def _client_with_store_and_transport(
    app_config: ServiceConfig, store: InMemoryRuntimeConfigStore
) -> tuple[TestClient, FakeCuratedSourceTransport]:
    """A `TestClient` whose `GET /catalog` resolves the override through `store`."""
    app = create_app(app_config)
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))

    def _resolve(config: ServiceConfig) -> EffectiveCatalogSource:
        return resolve_effective_source(config, store=store)

    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport, resolve_effective_source=_resolve)
    )
    return TestClient(app), transport


def test_get_catalog_uses_the_override_set_via_mcp_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-007: `set-catalog-source` takes effect on the very next `GET /catalog` call."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    _call("set-catalog-source", {"url": _OVERRIDE_URL})

    client, transport = _client_with_store_and_transport(_app_config(), store)
    response = client.get("/catalog")

    assert response.status_code == 200
    assert {item["instrument_id"] for item in response.json()["instruments"]} == {"CRA-1.0"}
    assert [request.full_url for request in transport.requests] == [f"{_OVERRIDE_URL}/catalog.json"]


def test_get_catalog_reverts_to_default_after_reset_catalog_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008, end to end: `reset-catalog-source` makes `GET /catalog` use the default again."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    store = _install_store(monkeypatch)
    _call("set-catalog-source", {"url": _OVERRIDE_URL})
    _call("reset-catalog-source")

    assert resolve_effective_source(_app_config(), store=store) == EffectiveCatalogSource(
        url=_DEFAULT_URL, is_override=False
    )
    client, transport = _client_with_store_and_transport(_app_config(), store)
    assert client.get("/catalog").status_code == 200
    assert [request.full_url for request in transport.requests] == [f"{_DEFAULT_URL}/catalog.json"]


def test_get_catalog_fails_closed_when_the_override_read_fails() -> None:
    """AC-BI-010 via `GET /catalog`: named 503, never the env-var/default source."""
    configure()
    store = InMemoryRuntimeConfigStore(_app_config(), fail_reads=True)

    client, transport = _client_with_store_and_transport(_app_config(), store)
    response = client.get("/catalog")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_source_override_unavailable"
    assert transport.requests == []
