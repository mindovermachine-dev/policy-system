"""`falkordb_live` proof for `tools/curated-export/export_instrument.py` (issue #66, D4).

Runs the real CLI shim's `main()` end to end against a real, throwaway
`{short}_baseline`/`{short}_native` FalkorDB graph pair -- uniquely tokened
and deleted in a `finally` block, matching this suite's own established
`falkordb_live` convention (e.g. `test_serialize_live.py`), unlike
`test_engineering_practices_migration_live.py`'s deliberate exception for the
real, permanent `engprac_*` graphs.

No `llm_live` marker and no real LLM provider call: `main()`'s `call_embedding`
keyword is a test-only injection seam (module docstring of the CLI shim
itself) used here to pass a deterministic fake `EmbeddingCaller`, exactly
like `test_engineering_practices_migration_live.py`'s own
`_FakeEmbeddingCaller`. This still exercises every other real part of the
pipeline: real FalkorDB reads/writes, real argument parsing, real graph-name
resolution (`baseline_graph_name`/`native_graph_name`), real file output on
disk, and the real `catalog.json` regeneration (including its packaged
copy).
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

if TYPE_CHECKING:
    from types import ModuleType

    from falkordb import FalkorDB

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "tools" / "curated-export" / "export_instrument.py"
)


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_export_instrument_cli_under_test_live", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEmbeddingCaller:
    """A deterministic embedding stand-in -- no `llm_live` marker, no real provider call."""

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


@pytest.mark.falkordb_live
def test_export_instrument_cli_runs_end_to_end_against_a_real_throwaway_graph_pair(
    live_falkordb: FalkorDB, tmp_path: Path
) -> None:
    token = uuid.uuid4().hex[:10]
    short_name = f"AC66CLI{token}".upper()
    baseline_name = f"{short_name.lower()}_baseline"
    native_name = f"{short_name.lower()}_native"
    instrument_id = f"TEST-{token}"

    live_falkordb.select_graph(baseline_name).query(
        "CREATE (:RegulatoryInstrument {id: $id, source_type: 'external'}), "
        "(:Capability {id: 'cap1', name: 'Cap One', confidence: 1.0})",
        {"id": instrument_id},
    )
    live_falkordb.select_graph(native_name).query(
        "CREATE (:RegulatoryInstrument {id: $id})", {"id": instrument_id}
    )

    cli_module = _load_cli_module()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    fake_caller = _FakeEmbeddingCaller()

    try:
        argv = [
            "--short-name",
            short_name,
            "--instrument-id",
            instrument_id,
            "--version",
            "1.0",
            "--title",
            "CLI Live Test Instrument",
            "--source-type",
            "external",
            "--celex",
            "32099R0001",
            "--jurisdiction",
            "EU",
            "--embed-model",
            "fake-embed-model",
            "--repo-root",
            str(repo_root),
            "--packaged-copy-path",
            str(packaged_copy_path),
        ]

        exit_code = cli_module.main(argv, call_embedding=fake_caller)

        assert exit_code == 0
        assert fake_caller.calls == ["Cap One"]

        instrument_dir = repo_root / "curated-content" / instrument_id
        manifest = json.loads((instrument_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["instrument_id"] == instrument_id
        assert manifest["short_name"] == short_name
        assert manifest["source_type"] == "external"

        baseline_doc = json.loads((instrument_dir / "baseline.json").read_text(encoding="utf-8"))
        capability_nodes = [n for n in baseline_doc["nodes"] if n["label"] == "Capability"]
        assert len(capability_nodes) == 1
        assert "embedding" in capability_nodes[0]["properties"]

        native_doc = json.loads((instrument_dir / "native.json").read_text(encoding="utf-8"))
        assert any(n["label"] == "RegulatoryInstrument" for n in native_doc["nodes"])

        assert packaged_copy_path.is_file()
        catalog = json.loads(packaged_copy_path.read_text(encoding="utf-8"))
        assert any(entry["instrument_id"] == instrument_id for entry in catalog)
        root_catalog = json.loads((repo_root / "curated-content" / "catalog.json").read_text())
        assert any(entry["instrument_id"] == instrument_id for entry in root_catalog)
    finally:
        live_falkordb.connection.delete(baseline_name, native_name)
        assert live_falkordb.connection.exists(baseline_name) == 0
        assert live_falkordb.connection.exists(native_name) == 0


@pytest.mark.falkordb_live
def test_export_instrument_cli_fails_loudly_when_falkordb_is_unreachable(
    tmp_path: Path,
) -> None:
    """Not a real connectivity gap -- proves the CLI's own connection guard against a
    real, deliberately-wrong port, without needing to stop the real FalkorDB instance.
    """
    cli_module = _load_cli_module()
    argv = [
        "--short-name",
        "NOPE",
        "--instrument-id",
        "NOPE-1",
        "--version",
        "1.0",
        "--title",
        "Unreachable",
        "--source-type",
        "external",
        "--embed-model",
        "fake-embed-model",
        "--host",
        "127.0.0.1",
        "--port",
        "1",
        "--repo-root",
        str(tmp_path / "repo"),
    ]

    exit_code = cli_module.main(argv, call_embedding=_FakeEmbeddingCaller())

    assert exit_code == 1
