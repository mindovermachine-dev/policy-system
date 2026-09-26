"""Tests for `ps_service.curated_source.resolve.resolve_effective_source` (issue #125, Slice 3).

Covers AC-BI-013 (a persisted override takes precedence over the env-var/
default) and D-FAILOPEN (any exception opening/querying the graph for the
override falls through to the env-var/default, logged, never raised to the
caller). Uses an injectable, hand-written fake `open_graph`/`GraphHandle` --
no real FalkorDB needed for any test in this file (PLAN.md Slice 3's own
scoping of this file).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.config import ServiceConfig
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines

_DEFAULT_URL = "https://example.com/default-source"
_OVERRIDE_URL = "https://example.com/persisted-override"


def _config(*, default_url: str = _DEFAULT_URL) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        curated_source_base_url=default_url,
    )


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeOverrideGraph:
    """A minimal fake satisfying `curated_source.store.GraphHandle` for the singleton read."""

    def __init__(self, url: str | None) -> None:
        self.url = url
        self.query_count = 0

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        _ = (q, params)
        self.query_count += 1
        return _FakeQueryResult([[self.url]] if self.url is not None else [])


def test_resolve_returns_persisted_override_when_present() -> None:
    """AC-BI-013: a persisted override wins over the env-var/default."""
    graph = _FakeOverrideGraph(_OVERRIDE_URL)
    config = _config()

    result = resolve_effective_source(config, open_graph=lambda: graph)

    assert result == EffectiveCatalogSource(url=_OVERRIDE_URL, is_override=True)


def test_resolve_falls_back_to_default_when_no_override_persisted() -> None:
    """No `CatalogSourceOverride` node exists -- the env-var/default value is used."""
    graph = _FakeOverrideGraph(None)
    config = _config()

    result = resolve_effective_source(config, open_graph=lambda: graph)

    assert result == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)


def test_resolve_falls_back_and_logs_warning_when_opening_the_graph_raises(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """D-FAILOPEN: an unreachable FalkorDB during the override check never raises to the caller."""
    emitter, log_path = make_emitter()
    config = _config()

    def _raising_open_graph() -> _FakeOverrideGraph:
        message = "connection refused"
        raise ConnectionError(message)

    result = resolve_effective_source(config, open_graph=_raising_open_graph, emitter=emitter)

    assert result == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)
    emitter.flush()
    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["component"] == "curated_source"
    assert entries[0]["action"] == "resolve_effective_source"
    assert entries[0]["outcome"] == "fallback"
    assert "connection refused" in str(entries[0]["reason"])


def test_resolve_falls_back_and_logs_warning_when_reading_the_override_raises(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """D-FAILOPEN also covers a query-time failure (graph opened, but the read itself fails)."""
    emitter, log_path = make_emitter()
    config = _config()

    class _RaisingGraph:
        def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
            _ = (q, params)
            message = "FalkorDB query timed out"
            raise TimeoutError(message)

    result = resolve_effective_source(config, open_graph=_RaisingGraph, emitter=emitter)

    assert result == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)
    emitter.flush()
    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "fallback"
    assert "FalkorDB query timed out" in str(entries[0]["reason"])


def test_resolve_effective_source_recovers_override_precedence_after_transient_falkordb_outage(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """CHANGES.md Appendix A1: the fail-open is per-call/transient, not a sticky bypass.

    Two sequential calls against the SAME persisted override:

    1. `open_graph` raises (simulated FalkorDB outage) -> falls back to the
       env-var/default, `is_override is False`, and a warning is logged.
    2. Immediately after, `open_graph` now succeeds (no exception) and the
       same override is still persisted from before the outage -> the
       persisted override URL is returned, `is_override is True`.

    Together this proves the fallback never permanently suppresses a live
    override once FalkorDB has recovered.
    """
    emitter, log_path = make_emitter()
    config = _config()
    persisted_graph = _FakeOverrideGraph(_OVERRIDE_URL)
    outage_active = True

    def _open_graph() -> _FakeOverrideGraph:
        if outage_active:
            message = "simulated transient FalkorDB outage"
            raise ConnectionError(message)
        return persisted_graph

    first = resolve_effective_source(config, open_graph=_open_graph, emitter=emitter)
    assert first == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)

    outage_active = False
    second = resolve_effective_source(config, open_graph=_open_graph, emitter=emitter)
    assert second == EffectiveCatalogSource(url=_OVERRIDE_URL, is_override=True)

    emitter.flush()
    entries = read_lines(log_path)
    fallback_entries = [entry for entry in entries if entry.get("outcome") == "fallback"]
    assert len(fallback_entries) == 1
    assert fallback_entries[0]["component"] == "curated_source"
    assert fallback_entries[0]["action"] == "resolve_effective_source"
    assert "simulated transient FalkorDB outage" in str(fallback_entries[0]["reason"])
