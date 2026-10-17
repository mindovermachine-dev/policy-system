"""Real-wiring proof for `tools/curated-export/export_instrument.py`.

This suite never touches a real FalkorDB or Postgres and never calls a real LLM provider, but it
*does* exercise the real `ps_service.export.export_instrument.export_instrument` orchestrator
(embed -> serialize -> checksum -> manifest -> catalog.json) end to end. The Graph Write Gateway
rig (in-memory log and FalkorDB, the true boundaries) is what the CLI's gateway, read handles and
system audit recorder are; a fake `EmbeddingCaller` stands in for the LLM Provider -- so every
assertion below is against real written files and what the rig's graph, log and audit recorder
hold, never a captured-kwargs stub. The CLI writes the embedding backfill through
a gateway it builds from the configuration (with `--host`/`--port` applied), after recording the
Export system audit row.

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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from api._gateway_fakes import RecordingSystemAudit
from graph_gateway._fakes import GatewayRig
from litellm.types.utils import Embedding, EmbeddingResponse

from export._gateway_fakes import (
    ExportReadGraph,
    ExportRig,
    ScriptedPolicyGraph,
    pending_review,
)
from ps_service.api.errors import ExportUnresolvedNearMissesError
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.company_merge.falkordb_client import single_tenant_graph_name
from ps_service.config import ServiceConfig
from ps_service.logging import facade

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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


class _FakeFalkorDB:
    """Records `select_graph` calls; each name is a read handle over that rig graph.

    The single-tenant `policy_system` name answers with `policy_graph` when one is scripted;
    otherwise that graph does not exist (`list_graphs` omits it) as on a fresh server.
    """

    def __init__(self, *, rig: GatewayRig, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.selected: list[str] = []
        self.policy_graph: ScriptedPolicyGraph | None = None
        self._rig = rig

    def list_graphs(self) -> list[str]:
        names = list(self._rig.graphs.graphs)
        if self.policy_graph is not None:
            names.append(single_tenant_graph_name())
        return names

    def select_graph(self, name: str) -> ExportReadGraph | ScriptedPolicyGraph:
        self.selected.append(name)
        if name == single_tenant_graph_name():
            assert self.policy_graph is not None, "policy_system must not be selected when absent"
            return self.policy_graph
        return ExportReadGraph(self._rig.graphs.open(name))


def _constant_falkordb_ctor(fake_db: _FakeFalkorDB) -> Callable[..., _FakeFalkorDB]:
    """A typed `FalkorDB(host=..., port=...)`-shaped constructor stand-in always
    returning the same pre-built `fake_db` -- avoids an untyped lambda (basedpyright
    can't infer `*, host, port`'s parameter types from a bare lambda).
    """

    def _ctor(*, host: str, port: int) -> _FakeFalkorDB:
        return fake_db

    return _ctor


class _Wiring:
    """What the CLI's composition root is patched to: the rig's gateway and an audit recorder."""

    def __init__(self, rig: ExportRig, system_audit: RecordingSystemAudit) -> None:
        self.rig = rig
        self.system_audit = system_audit
        self.gateway_configs: list[ServiceConfig] = []
        self.fake_db = _FakeFalkorDB(rig=rig.rig, host="fake-host", port=1234)

    def load_config(self) -> ServiceConfig:
        """Stand-in for `load_config`: a fixed configuration, no environment read."""
        return ServiceConfig(
            host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
        )

    def main(self, cli_module: ModuleType, argv: list[str], **kwargs: object) -> int:
        """Run the CLI's `main()` with this rig injected at its composition seams."""
        return int(
            cli_module.main(
                argv,
                load_service_config=self.load_config,
                build_gateway=self.build_gateway,
                build_audit=self.build_audit,
                **kwargs,
            )
        )

    def build_gateway(self, config: ServiceConfig) -> object:
        """Stand-in for `build_default_graph_write_gateway`: records the config it was given."""
        self.gateway_configs.append(config)
        return self.rig.rig.gateway

    def build_audit(self, config: ServiceConfig) -> RecordingSystemAudit:
        """Stand-in for `build_system_audit_recorder`."""
        del config
        return self.system_audit


@pytest.fixture
def wiring(cli_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Wiring]:
    """A `cra_*` rig, with the CLI's FalkorDB patched to it."""
    gateway_rig = GatewayRig()
    export_rig = ExportRig(gateway_rig, short_name="CRA")
    wired = _Wiring(export_rig, RecordingSystemAudit(gateway_rig))
    monkeypatch.setattr(cli_module, "FalkorDB", _constant_falkordb_ctor(wired.fake_db))
    yield wired
    gateway_rig.close()


def _seed_instrument(rig: ExportRig, instrument_id: str, *, source_type: str | None) -> None:
    properties: dict[str, object] = {}
    if source_type is not None:
        properties["source_type"] = source_type
    rig.seed_node(rig.baseline, "RegulatoryInstrument", instrument_id, properties)
    rig.seed_node(rig.native, "RegulatoryInstrument", instrument_id, {})


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
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
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
    rig = wiring.rig
    _seed_instrument(rig, instrument_id, source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
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
    exit_code = wiring.main(cli_module, argv, call_embedding=fake_caller)

    assert exit_code == 0

    # `select_graph("cra_baseline")` is called twice: once for the connectivity probe,
    # once to build `baseline_graph` -- then once more for `native_graph`.
    assert wiring.fake_db.selected == ["cra_baseline", "cra_baseline", "cra_native"]
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


def test_main_writes_the_backfill_as_one_gateway_group_anchored_to_the_audit_row(
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
) -> None:
    """No direct write; one `cra_baseline` group, anchored to the row the
    CLI recorded first (the read handle refuses any write statement).
    """
    instrument_id = "32024R2847"
    rig = wiring.rig
    _seed_instrument(rig, instrument_id, source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})

    exit_code = wiring.main(
        cli_module,
        [
            *_BASE_ARGV,
            "--repo-root",
            str(tmp_path / "repo"),
            "--packaged-copy-path",
            str(tmp_path / "p.json"),
        ],
        call_embedding=_FakeEmbeddingCaller(),
    )

    assert exit_code == 0
    assert len(wiring.system_audit.rows) == 1
    row = wiring.system_audit.rows[0]
    assert (row["action"], row["resource_type"], row["resource_id"]) == (
        "export.instrument",
        "instrument",
        instrument_id,
    )
    groups = rig.baseline_groups()
    assert len(groups) == 1
    assert groups[0].audit_event_id == wiring.system_audit.ids[0]
    assert [(e.name, e.identity) for e in groups[0].entries] == [("Capability", "cap1")]
    assert rig.rig.store.groups == groups  # the native graph is never written


def test_main_builds_its_gateway_for_the_host_and_port_it_reads(
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
) -> None:
    """`--host`/`--port` reach the gateway's FalkorDB too, so reads and writes hit one server."""
    _seed_instrument(wiring.rig, "32024R2847", source_type="external")

    exit_code = wiring.main(
        cli_module,
        [
            *_BASE_ARGV,
            "--repo-root",
            str(tmp_path / "repo"),
            "--packaged-copy-path",
            str(tmp_path / "p.json"),
        ],
        call_embedding=_FakeEmbeddingCaller(),
    )

    assert exit_code == 0
    assert [(c.falkordb_host, c.falkordb_port) for c in wiring.gateway_configs] == [
        ("fake-host", 1234)
    ]


def test_main_exits_non_zero_and_writes_nothing_when_the_audit_row_cannot_be_written(
    cli_module: ModuleType,
    wiring: _Wiring,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail-closed -- no embedding is computed, no group is submitted."""
    instrument_id = "32024R2847"
    rig = wiring.rig
    _seed_instrument(rig, instrument_id, source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
    wiring.system_audit = RecordingSystemAudit(
        rig.rig, error=AuditTrailUnavailableError("The audit trail is temporarily unavailable.")
    )
    caller = _FakeEmbeddingCaller()

    exit_code = wiring.main(
        cli_module,
        [
            *_BASE_ARGV,
            "--repo-root",
            str(tmp_path / "repo"),
            "--packaged-copy-path",
            str(tmp_path / "p.json"),
        ],
        call_embedding=caller,
    )

    assert exit_code == 1
    assert "audit" in capsys.readouterr().err.lower()
    assert caller.calls == []
    assert rig.rig.store.groups == []
    assert not (tmp_path / "repo").exists()


def _refusal_argv(tmp_path: Path) -> list[str]:
    return [
        *_BASE_ARGV,
        "--repo-root",
        str(tmp_path / "repo"),
        "--packaged-copy-path",
        str(tmp_path / "p.json"),
    ]


def test_main_refuses_with_exit_1_and_writes_nothing_while_near_misses_are_unresolved(
    cli_module: ModuleType,
    wiring: _Wiring,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 1, count and review ids on stderr, no files, no audit row, no gateway."""
    rig = wiring.rig
    _seed_instrument(rig, "32024R2847", source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
    wiring.fake_db.policy_graph = ScriptedPolicyGraph(
        [
            pending_review("review-b", incoming_id="cap1"),
            pending_review("review-a", nearest_existing_id="cap1"),
            pending_review("review-other", incoming_id="elsewhere"),
        ]
    )
    caller = _FakeEmbeddingCaller()

    exit_code = wiring.main(cli_module, _refusal_argv(tmp_path), call_embedding=caller)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.strip() == str(
        ExportUnresolvedNearMissesError("32024R2847", ("review-a", "review-b"))
    )
    assert "2 unresolved near-miss reviews" in err
    assert caller.calls == []
    assert wiring.gateway_configs == []
    assert wiring.system_audit.rows == []
    assert rig.rig.store.groups == []
    assert not (tmp_path / "repo").exists()
    assert not (tmp_path / "p.json").exists()


def test_main_exports_when_policy_system_holds_only_other_instruments_reviews(
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
) -> None:
    """Reviews that touch no baseline Capability id do not block the export."""
    rig = wiring.rig
    _seed_instrument(rig, "32024R2847", source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
    wiring.fake_db.policy_graph = ScriptedPolicyGraph(
        [pending_review("review-other", incoming_id="elsewhere")]
    )

    exit_code = wiring.main(
        cli_module, _refusal_argv(tmp_path), call_embedding=_FakeEmbeddingCaller()
    )

    assert exit_code == 0
    assert (tmp_path / "repo" / "curated-content" / "catalog.json").exists()


def test_main_skips_the_gate_and_creates_no_graph_when_policy_system_is_absent(
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
) -> None:
    """An absent `policy_system` holds no review and is never selected (that would create it)."""
    _seed_instrument(wiring.rig, "32024R2847", source_type="external")
    assert wiring.fake_db.policy_graph is None

    exit_code = wiring.main(
        cli_module, _refusal_argv(tmp_path), call_embedding=_FakeEmbeddingCaller()
    )

    assert exit_code == 0
    assert single_tenant_graph_name() not in wiring.fake_db.selected


def test_main_fails_closed_when_policy_system_cannot_be_read(
    cli_module: ModuleType,
    wiring: _Wiring,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A read failure exits 1 with a message and writes nothing."""
    rig = wiring.rig
    _seed_instrument(rig, "32024R2847", source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
    wiring.fake_db.policy_graph = ScriptedPolicyGraph(fail_with=ConnectionError("boom"))
    caller = _FakeEmbeddingCaller()

    exit_code = wiring.main(cli_module, _refusal_argv(tmp_path), call_embedding=caller)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert single_tenant_graph_name() in err
    assert "boom" in err
    assert caller.calls == []
    assert wiring.system_audit.rows == []
    assert not (tmp_path / "repo").exists()


def test_main_exits_non_zero_when_the_gateway_cannot_write_the_group(
    cli_module: ModuleType,
    wiring: _Wiring,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gateway failure (here the log) is reported and exits non-zero; no artifact is written."""
    instrument_id = "32024R2847"
    rig = wiring.rig
    _seed_instrument(rig, instrument_id, source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})
    rig.rig.store.fail_appends()

    exit_code = wiring.main(
        cli_module,
        [
            *_BASE_ARGV,
            "--repo-root",
            str(tmp_path / "repo"),
            "--packaged-copy-path",
            str(tmp_path / "p.json"),
        ],
        call_embedding=_FakeEmbeddingCaller(),
    )

    assert exit_code == 1
    assert "write" in capsys.readouterr().err.lower()
    assert rig.embedding_of("Capability", "cap1") is None
    assert not (tmp_path / "repo").exists()


def test_main_honors_explicit_repo_root_and_packaged_copy_path(
    cli_module: ModuleType, wiring: _Wiring, tmp_path: Path
) -> None:
    instrument_id = "32024R2847"
    _seed_instrument(wiring.rig, instrument_id, source_type=None)
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
    exit_code = wiring.main(cli_module, [*_BASE_ARGV, *extra_argv], call_embedding=object())

    assert exit_code == 0
    instrument_dir = repo_root / "curated-content" / instrument_id
    assert (instrument_dir / "manifest.json").is_file()
    assert (instrument_dir / "baseline.json").is_file()
    assert (instrument_dir / "native.json").is_file()
    assert packaged_copy_path.is_file()
    assert (repo_root / "curated-content" / "catalog.json").is_file()
    assert wiring.rig.rig.store.groups == []  # nothing to embed -> no group


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
    cli_module: ModuleType, wiring: _Wiring, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed embedding response (`data=[]`) makes `route_embedding`'s own
    `_to_embedding_result` raise `LlmProviderError` for real -- `export_instrument`
    is not patched; the failure is real backfill-time behavior, injected only at
    the CLI's own documented `call_embedding` test seam (module docstring).
    """
    instrument_id = "32024R2847"
    rig = wiring.rig
    _seed_instrument(rig, instrument_id, source_type="external")
    rig.seed_node(rig.baseline, "Capability", "cap1", {"name": "Cap One"})

    exit_code = wiring.main(cli_module, _BASE_ARGV, call_embedding=_EmptyEmbeddingResponseCaller())

    assert exit_code == 1
    assert "llm provider error" in capsys.readouterr().err.lower()
    # The backfill never got far enough to write anything back onto the node.
    assert rig.embedding_of("Capability", "cap1") is None
    assert rig.rig.store.groups == []
