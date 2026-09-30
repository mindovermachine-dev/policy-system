"""Real-wiring proof for `tools/curated-export/export_instrument.py` (issue #66, D4).

This suite never touches a real FalkorDB instance and never calls a real LLM
provider, but it *does* exercise the real `ps_service.export.export_instrument.
export_instrument` orchestrator (embed -> serialize -> checksum -> manifest ->
catalog.json) end to end: a small, generic in-memory Cypher engine
(`_StatefulFakeGraph`) stands in for FalkorDB at the true `_GraphQueryHandle`
boundary (mirrors `test_restore_instrument_audit_log.py`'s
`_FakeStagedGraph` regex-dispatcher convention, `IMPL_SLICE_8.md`), and a
fake `EmbeddingCaller` stands in for the LLM Provider (D7) -- so every
assertion below is against real written files/catalog state, never a
captured-kwargs stub.

`tools/` is outside pytest's `testpaths` (root `pyproject.toml`) and carries
no `__init__.py` (a standalone script, not a package member) -- the CLI
module is loaded by file path via `importlib.util`, the standard way to
import a non-package module by location.

The real, real-FalkorDB end-to-end proof (real FalkorDB, real files on disk,
a fake embedding caller standing in for the LLM Provider) is
`test_export_instrument_cli_live.py`'s `falkordb_live` test.
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.logging import facade

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import ModuleType

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "curated-export" / "export_instrument.py"
)


@pytest.fixture(autouse=True)
def _preserve_atexit_registered_guard() -> (  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    Iterator[None]
):
    """Every test here drives `cli_module.main()`, which calls the real
    `Logging.configure()` (see `export_instrument.py`'s own `configure_logging()`
    call). `configure()` registers an `atexit` drain hook exactly once per
    *process* (D9) via the module-global `_atexit_registered` guard, and the
    root `tests/conftest.py`'s autouse `reset_for_tests()` deliberately does
    not clear that guard (it only nulls the default emitter).

    Without saving/restoring it here, a real `configure()` call in this file
    would permanently flip the guard for the rest of the pytest process and
    poison `tests/logging/test_facade_emit_log_entry.py`'s
    `test_atexit_drain_hook_registered_once_when_configure_called_multiple_times`,
    which asserts `atexit.register` runs exactly once from a clean start --
    exactly the same risk `tests/api/conftest.py`'s `configured_logging`
    fixture already guards against for `tests/api`.
    """
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = (  # pyright: ignore[reportPrivateUsage]
            saved_atexit_registered
        )


def _load_cli_module() -> ModuleType:
    """Import the CLI shim fresh, by file path -- see module docstring."""
    spec = importlib.util.spec_from_file_location(
        "_export_instrument_cli_under_test_fake_wiring", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli_module() -> ModuleType:
    return _load_cli_module()


class _FakeQueryResult:
    """Structural stand-in for `falkordb.QueryResult` -- the one field read anywhere."""

    def __init__(self, result_set: list[list[object]]) -> None:
        self.result_set = result_set


@dataclass
class _FakeNode:
    label: str
    properties: dict[str, object]


# The fixed query shapes `ps_service.export.serialize.serialize_graph` and
# `ps_service.export.embeddings.backfill_capability_embeddings` issue against an
# already-selected graph -- the only two real collaborators `export_instrument`
# talks to through `_GraphQueryHandle`. No edge-query pattern: none of this
# file's fixtures create any relationship, and `CALL db.relationshipTypes()`
# returning `[]` means `serialize_graph` never issues an edge query at all.
_NODE_ROWS_PATTERN = re.compile(
    r"\AMATCH \(n:(?P<label>\w+)\) RETURN labels\(n\), properties\(n\)\Z"
)
_MISSING_EMBEDDING_PATTERN = re.compile(
    r"\AMATCH \(n:(?P<label>\w+)\) WHERE n\.embedding IS NULL RETURN n\.id, n\.(?P<prop>\w+)\Z"
)
_WRITE_EMBEDDING_PATTERN = re.compile(
    r"\AMATCH \(n:(?P<label>\w+) \{id: \$id\}\) SET n\.embedding = \$embedding\Z"
)


class _StatefulFakeGraph:
    """A tiny in-memory FalkorDB-shaped Cypher engine.

    One small, generic dispatcher recognizing exactly the query shapes the
    real orchestrator issues, instead of per-label hardcoding -- mirrors
    `test_restore_instrument_audit_log.py`'s `_FakeStagedGraph` regex-
    dispatcher convention (`IMPL_SLICE_8.md`). Real `export_instrument` code
    runs against this: `serialize_graph`'s label/count enumeration and
    per-label `MATCH`, and `backfill_capability_embeddings`'s missing-
    embedding read plus embedding write, all execute for real.
    """

    def __init__(self, name: str, *, nodes: Sequence[_FakeNode] = ()) -> None:
        self.name = name
        self._nodes = list(nodes)

    def properties_of(self, label: str, node_id: str) -> dict[str, object]:
        """Test-assertion helper: the current (possibly since-mutated) properties
        of one node, by label + id.
        """
        for node in self._nodes:
            if node.label == label and node.properties.get("id") == node_id:
                return node.properties
        message = f"no {label} node with id={node_id!r}"
        raise AssertionError(message)

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        params = params or {}
        if q == "RETURN 1":
            return _FakeQueryResult([[1]])
        if q == "CALL db.labels()":
            return _FakeQueryResult([[label] for label in sorted({n.label for n in self._nodes})])
        if q == "CALL db.relationshipTypes()":
            return _FakeQueryResult([])
        if q == "MATCH (n) RETURN count(n)":
            return _FakeQueryResult([[len(self._nodes)]])
        if match := _NODE_ROWS_PATTERN.match(q):
            label = match["label"]
            return _FakeQueryResult(
                [[[n.label], dict(n.properties)] for n in self._nodes if n.label == label]
            )
        if match := _MISSING_EMBEDDING_PATTERN.match(q):
            label, prop = match["label"], match["prop"]
            return _FakeQueryResult(
                [
                    [n.properties["id"], n.properties.get(prop)]
                    for n in self._nodes
                    if n.label == label and "embedding" not in n.properties
                ]
            )
        if match := _WRITE_EMBEDDING_PATTERN.match(q):
            label = match["label"]
            node_id = params["id"]
            for n in self._nodes:
                if n.label == label and n.properties.get("id") == node_id:
                    n.properties["embedding"] = params["embedding"]
            return _FakeQueryResult([])
        message = f"_StatefulFakeGraph: unrecognized query {q!r}"
        raise AssertionError(message)


class _FakeFalkorDB:
    """Records `select_graph` calls; each name maps to one real, stateful in-memory graph."""

    def __init__(
        self, *, host: str, port: int, graphs: dict[str, _StatefulFakeGraph] | None = None
    ) -> None:
        self.host = host
        self.port = port
        self.selected: list[str] = []
        self._graphs = dict(graphs) if graphs is not None else {}

    def select_graph(self, name: str) -> _StatefulFakeGraph:
        self.selected.append(name)
        return self._graphs.setdefault(name, _StatefulFakeGraph(name))


def _constant_falkordb_ctor(fake_db: _FakeFalkorDB) -> Callable[..., _FakeFalkorDB]:
    """A typed `FalkorDB(host=..., port=...)`-shaped constructor stand-in always
    returning the same pre-built `fake_db` -- avoids an untyped lambda (basedpyright
    can't infer `*, host, port`'s parameter types from a bare lambda).
    """

    def _ctor(*, host: str, port: int) -> _FakeFalkorDB:
        return fake_db

    return _ctor


def _regulatory_instrument_node(instrument_id: str, *, source_type: str | None = None) -> _FakeNode:
    properties: dict[str, object] = {"id": instrument_id}
    if source_type is not None:
        properties["source_type"] = source_type
    return _FakeNode(label="RegulatoryInstrument", properties=properties)


def _capability_node(node_id: str, name: str) -> _FakeNode:
    return _FakeNode(label="Capability", properties={"id": node_id, "name": name})


class _FakeEmbeddingCaller:
    """Deterministic embedding stand-in for the real LLM Provider boundary --
    same shape as `test_export_instrument_cli_live.py`'s own `_FakeEmbeddingCaller`
    (D7's one real-LLM-Provider call in the whole feature stays test-doubled here;
    the `falkordb_live` sibling test is this file's real-FalkorDB counterpart).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append((model, text))
        vector = [float(len(text) % 7), 0.5]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


class _EmptyEmbeddingResponseCaller:
    """A malformed-response stand-in: `route_embedding`'s own `_to_embedding_result`
    naturally raises `LlmProviderError` for an empty `data` list -- proves the CLI's
    `LlmProviderError` handling without patching `export_instrument` itself.
    """

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        return EmbeddingResponse(model=model, data=[])


_BASE_ARGV = [
    "--short-name",
    "CRA",
    "--instrument-id",
    "32024R2847",
    "--version",
    "1.0",
    "--title",
    "Cyber Resilience Act",
    "--source-type",
    "external",
    "--celex",
    "32024R2847",
    "--jurisdiction",
    "EU",
    "--host",
    "fake-host",
    "--port",
    "1234",
    "--embed-model",
    "azure/text-embedding-3-large",
]


def test_main_wires_cli_args_into_export_instrument(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every descriptor field, both graph selections, and every passthrough kwarg
    reach the real `export_instrument` orchestrator -- proven by its real written
    output (manifest.json/baseline.json/native.json/catalog.json), not by a
    captured-kwargs stub. `--repo-root`/`--packaged-copy-path` are passed
    explicitly, into `tmp_path`, so a real run never writes into this checkout's
    own working tree -- the CLI's own default-value passthrough for these two
    flags is plain `argparse` wiring, not this orchestrator-focused test's concern.
    """
    instrument_id = "32024R2847"
    baseline = _StatefulFakeGraph(
        "cra_baseline",
        nodes=[
            _regulatory_instrument_node(instrument_id, source_type="external"),
            _capability_node("cap1", "Cap One"),
        ],
    )
    native = _StatefulFakeGraph("cra_native", nodes=[_regulatory_instrument_node(instrument_id)])
    fake_db = _FakeFalkorDB(
        host="fake-host", port=1234, graphs={"cra_baseline": baseline, "cra_native": native}
    )
    # This CLI shim's own top-level infra-connection boundary (PLAN.md §1.4
    # thin-FalkorDB-wrapper rule; `falkordb.FalkorDB` itself is an approved-mock-boundaries.yaml
    # entry -- see its `falkordb:` section -- covering this raw-import call site.
    monkeypatch.setattr(cli_module, "FalkorDB", _constant_falkordb_ctor(fake_db))

    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    fake_caller = _FakeEmbeddingCaller()

    argv = [
        *_BASE_ARGV,
        "--repo-root",
        str(repo_root),
        "--packaged-copy-path",
        str(packaged_copy_path),
    ]
    exit_code = cli_module.main(argv, call_embedding=fake_caller)

    assert exit_code == 0

    # `select_graph("cra_baseline")` is called twice: once for the connectivity probe,
    # once to build `baseline_graph` -- then once more for `native_graph`.
    assert fake_db.selected == ["cra_baseline", "cra_baseline", "cra_native"]
    assert fake_caller.calls == [("azure/text-embedding-3-large", "Cap One")]

    instrument_dir = repo_root / "curated-content" / instrument_id
    manifest = json.loads((instrument_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["short_name"] == "CRA"
    assert manifest["instrument_id"] == instrument_id
    assert manifest["version"] == "1.0"
    assert manifest["celex"] == instrument_id
    assert manifest["title"] == "Cyber Resilience Act"
    assert manifest["source_type"] == "external"
    assert manifest["jurisdiction"] == "EU"

    baseline_doc = json.loads((instrument_dir / "baseline.json").read_text(encoding="utf-8"))
    capability_nodes = [n for n in baseline_doc["nodes"] if n["label"] == "Capability"]
    assert len(capability_nodes) == 1
    assert capability_nodes[0]["properties"]["embedding"] == [0.0, 0.5]

    native_doc = json.loads((instrument_dir / "native.json").read_text(encoding="utf-8"))
    assert [n["label"] for n in native_doc["nodes"]] == ["RegulatoryInstrument"]

    assert packaged_copy_path.is_file()
    catalog = json.loads(packaged_copy_path.read_text(encoding="utf-8"))
    assert any(entry["instrument_id"] == instrument_id for entry in catalog)
    root_catalog = json.loads((repo_root / "curated-content" / "catalog.json").read_text())
    assert any(entry["instrument_id"] == instrument_id for entry in root_catalog)


def test_main_honors_explicit_repo_root_and_packaged_copy_path(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instrument_id = "32024R2847"
    baseline = _StatefulFakeGraph(
        "cra_baseline", nodes=[_regulatory_instrument_node(instrument_id)]
    )
    native = _StatefulFakeGraph("cra_native", nodes=[_regulatory_instrument_node(instrument_id)])
    fake_db = _FakeFalkorDB(
        host="fake-host", port=1234, graphs={"cra_baseline": baseline, "cra_native": native}
    )
    monkeypatch.setattr(cli_module, "FalkorDB", _constant_falkordb_ctor(fake_db))
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    extra_argv = [
        "--repo-root",
        str(repo_root),
        "--packaged-copy-path",
        str(packaged_copy_path),
    ]
    # No Capability node exists in this fixture, so the embedding backfill finds
    # nothing to embed and `call_embedding` is never invoked -- a non-callable
    # `object()` sentinel proves that directly (calling it would raise).
    exit_code = cli_module.main([*_BASE_ARGV, *extra_argv], call_embedding=object())

    assert exit_code == 0
    instrument_dir = repo_root / "curated-content" / instrument_id
    assert (instrument_dir / "manifest.json").is_file()
    assert (instrument_dir / "baseline.json").is_file()
    assert (instrument_dir / "native.json").is_file()
    assert packaged_copy_path.is_file()
    assert (repo_root / "curated-content" / "catalog.json").is_file()


def test_main_fails_loudly_without_an_embed_model(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D7: this script is the one place needing a real LLM Provider -- a missing
    model must refuse before ever touching FalkorDB or `export_instrument`.

    Neither `FalkorDB` nor `export_instrument` is patched here: the guard must
    short-circuit before either the real constructor or the real orchestrator is
    ever reached. If the guard regressed, `main()` would instead attempt a real
    `FalkorDB(host="fake-host", port=1234)` connection and fail there with a
    *different* stderr message ("FalkorDB connection failed...", no "embed") --
    so this test's own assertion distinguishes the real guard from that fallback,
    with no mock/stub needed at either boundary.
    """
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    argv = [a for a in _BASE_ARGV if a not in {"--embed-model", "azure/text-embedding-3-large"}]

    exit_code = cli_module.main(argv)

    assert exit_code == 1
    assert "embed" in capsys.readouterr().err.lower()


def test_main_fails_loudly_on_a_falkordb_connection_error(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`export_instrument` is not patched here: the top-level connection guard
    must raise and exit before the CLI ever reaches it -- a regression would
    instead attempt a real orchestration run against `_BrokenDB`'s unusable
    graph and surface a different failure than this test's expected message.
    """

    class _BrokenGraph:
        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            message = "connection refused"
            raise ConnectionError(message)

    class _BrokenDB:
        def __init__(self, *, host: str, port: int) -> None:
            pass

        def select_graph(self, name: str) -> _BrokenGraph:
            return _BrokenGraph()

    monkeypatch.setattr(cli_module, "FalkorDB", _BrokenDB)

    exit_code = cli_module.main(_BASE_ARGV)

    assert exit_code == 1
    assert "connection failed" in capsys.readouterr().err.lower()


def test_main_reports_an_llm_provider_error_and_exits_non_zero(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed embedding response (`data=[]`) makes `route_embedding`'s own
    `_to_embedding_result` raise `LlmProviderError` for real -- `export_instrument`
    is not patched; the failure is real backfill-time behavior, injected only at
    the CLI's own documented `call_embedding` test seam (module docstring).
    """
    instrument_id = "32024R2847"
    baseline = _StatefulFakeGraph(
        "cra_baseline",
        nodes=[
            _regulatory_instrument_node(instrument_id, source_type="external"),
            _capability_node("cap1", "Cap One"),
        ],
    )
    native = _StatefulFakeGraph("cra_native", nodes=[_regulatory_instrument_node(instrument_id)])
    fake_db = _FakeFalkorDB(
        host="fake-host", port=1234, graphs={"cra_baseline": baseline, "cra_native": native}
    )
    monkeypatch.setattr(cli_module, "FalkorDB", _constant_falkordb_ctor(fake_db))

    exit_code = cli_module.main(_BASE_ARGV, call_embedding=_EmptyEmbeddingResponseCaller())

    assert exit_code == 1
    assert "llm provider error" in capsys.readouterr().err.lower()
    # The backfill never got far enough to write anything back onto the node.
    assert "embedding" not in baseline.properties_of("Capability", "cap1")
