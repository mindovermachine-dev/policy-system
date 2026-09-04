"""Fake-wiring proof for `tools/curated-export/export_instrument.py` (issue #66, D4).

This suite never touches a real FalkorDB instance and never calls a real LLM
provider -- it proves only that the CLI shim's argument parsing wires
correctly into the real `ps_service.export.export_instrument.export_instrument`
call shape (correct `InstrumentDescriptor` fields, correct `{short}_baseline`/
`{short}_native` graph selection, correct pass-through of `embed_model`/
`repo_root`/`packaged_copy_path`/`call_embedding`), and that it fails loudly
before ever touching FalkorDB or the library function when misconfigured
(D7: this is the one script in the whole feature that needs a real LLM
Provider). The real end-to-end proof (real FalkorDB, real files on disk, a
fake embedding caller standing in for the LLM Provider) is
`test_export_instrument_cli_live.py`'s `falkordb_live` test.

`tools/` is outside pytest's `testpaths` (root `pyproject.toml`) and carries
no `__init__.py` (a standalone script, not a package member) -- the CLI
module is loaded by file path via `importlib.util`, the standard way to
import a non-package module by location.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "curated-export" / "export_instrument.py"
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


class _FakeGraphHandle:
    """Structural stand-in for what `graph_query_handle`/`db.select_graph(...)` returns."""

    def __init__(self, name: str) -> None:
        self.name = name

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        class _Result:
            result_set: ClassVar[list[list[object]]] = []

        return _Result()


class _FakeFalkorDB:
    """Records `select_graph` calls; `query("RETURN 1")` always succeeds."""

    def __init__(self, *, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.selected: list[str] = []

    def select_graph(self, name: str) -> _FakeGraphHandle:
        self.selected.append(name)
        return _FakeGraphHandle(name)


class _FakeManifest:
    """The minimal `InstrumentManifest`-shaped object `main`'s summary print reads."""

    instrument_id = "32024R2847"
    short_name = "CRA"
    version = "1.0"
    schema_version = "v1"
    baseline_sha256 = "deadbeef"
    native_sha256 = "cafef00d"


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
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every descriptor field, both graph selections, and every passthrough kwarg
    reach the real `export_instrument` call exactly as the CLI parsed them.
    """
    fake_db = _FakeFalkorDB(host="fake-host", port=1234)

    def _fake_falkordb_ctor(*, host: str, port: int) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(cli_module, "FalkorDB", _fake_falkordb_ctor)

    captured: dict[str, Any] = {}

    def _fake_export_instrument(descriptor: object, **kwargs: object) -> _FakeManifest:
        captured["descriptor"] = descriptor
        captured["kwargs"] = kwargs
        return _FakeManifest()

    monkeypatch.setattr(cli_module, "export_instrument", _fake_export_instrument)
    fake_caller = object()

    exit_code = cli_module.main(_BASE_ARGV, call_embedding=fake_caller)

    assert exit_code == 0
    descriptor = captured["descriptor"]
    assert descriptor.short_name == "CRA"
    assert descriptor.instrument_id == "32024R2847"
    assert descriptor.version == "1.0"
    assert descriptor.celex == "32024R2847"
    assert descriptor.title == "Cyber Resilience Act"
    assert descriptor.source_type == "external"
    assert descriptor.jurisdiction == "EU"

    # `select_graph("cra_baseline")` is called twice: once for the connectivity probe,
    # once to build `baseline_graph` -- then once more for `native_graph`.
    assert fake_db.selected == ["cra_baseline", "cra_baseline", "cra_native"]
    kwargs = captured["kwargs"]
    assert kwargs["baseline_graph"].name == "cra_baseline"
    assert kwargs["native_graph"].name == "cra_native"
    assert kwargs["embed_model"] == "azure/text-embedding-3-large"
    assert kwargs["call_embedding"] is fake_caller
    assert kwargs["repo_root"] == cli_module.DEFAULT_REPO_ROOT
    assert kwargs["packaged_copy_path"] == cli_module.DEFAULT_PACKAGED_COPY_PATH


def test_main_honors_explicit_repo_root_and_packaged_copy_path(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_db = _FakeFalkorDB(host="fake-host", port=1234)

    def _fake_falkordb_ctor(*, host: str, port: int) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(cli_module, "FalkorDB", _fake_falkordb_ctor)
    captured: dict[str, Any] = {}

    def _fake_export_instrument(descriptor: object, **kwargs: object) -> _FakeManifest:
        captured.update(kwargs)
        return _FakeManifest()

    monkeypatch.setattr(cli_module, "export_instrument", _fake_export_instrument)
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    extra_argv = [
        "--repo-root",
        str(repo_root),
        "--packaged-copy-path",
        str(packaged_copy_path),
    ]
    exit_code = cli_module.main([*_BASE_ARGV, *extra_argv], call_embedding=object())

    assert exit_code == 0
    assert captured["repo_root"] == repo_root
    assert captured["packaged_copy_path"] == packaged_copy_path


def test_main_fails_loudly_without_an_embed_model(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D7: this script is the one place needing a real LLM Provider -- a missing
    model must refuse before ever touching FalkorDB or `export_instrument`.
    """
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    called: list[object] = []

    def _record_falkordb_ctor(*, host: str, port: int) -> object:
        called.append("falkordb")
        return object()

    def _record_export_instrument(descriptor: object, **kwargs: object) -> _FakeManifest:
        called.append("export")
        return _FakeManifest()

    monkeypatch.setattr(cli_module, "FalkorDB", _record_falkordb_ctor)
    monkeypatch.setattr(cli_module, "export_instrument", _record_export_instrument)
    argv = [a for a in _BASE_ARGV if a not in {"--embed-model", "azure/text-embedding-3-large"}]

    exit_code = cli_module.main(argv)

    assert exit_code == 1
    assert called == []
    assert "embed" in capsys.readouterr().err.lower()


def test_main_fails_loudly_on_a_falkordb_connection_error(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
    called: list[object] = []

    def _record_export_instrument(descriptor: object, **kwargs: object) -> _FakeManifest:
        called.append("export")
        return _FakeManifest()

    monkeypatch.setattr(cli_module, "export_instrument", _record_export_instrument)

    exit_code = cli_module.main(_BASE_ARGV)

    assert exit_code == 1
    assert called == []
    assert "connection failed" in capsys.readouterr().err.lower()


def test_main_reports_an_llm_provider_error_and_exits_non_zero(
    cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_db = _FakeFalkorDB(host="fake-host", port=1234)

    def _fake_falkordb_ctor(*, host: str, port: int) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(cli_module, "FalkorDB", _fake_falkordb_ctor)

    def _raise_llm_error(descriptor: object, **kwargs: object) -> _FakeManifest:
        message = "RouteEmbedding failed for model 'azure/text-embedding-3-large': boom"
        raise cli_module.LlmProviderError(message)

    monkeypatch.setattr(cli_module, "export_instrument", _raise_llm_error)

    exit_code = cli_module.main(_BASE_ARGV)

    assert exit_code == 1
    assert "llm provider error" in capsys.readouterr().err.lower()
