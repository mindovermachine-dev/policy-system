"""Tests for `tools/company-merge/company_merge_similarity_sweep.py`'s curated-content
Capability loader (issue #100, PLAN.md Slice 1).

Loads the script by path via `importlib.util.spec_from_file_location`, exactly as
`test_similarity_threshold_dataset.py`/`test_similarity_threshold_sweep_scoring.py`
already do for this same script.

Covers:

- AC-BI-001: `_load_curated_content_capability_nodes()` reads
  `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` via
  `export.serialize.parse_serialized_graph_json`, returning all 778 real Capability
  nodes.
- AC-BI-003: a missing, malformed, or wrong-shaped `baseline.json` raises
  `CuratedContentCorpusError` -- never a silent empty/partial result.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_service.export.serialize import (
    parse_serialized_graph_json as real_parse_serialized_graph_json,
)
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "tools" / "company-merge" / "company_merge_similarity_sweep.py"
_MODULE_NAME = "_company_merge_similarity_sweep_curated_content_loader_under_test"


def _load_sweep_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


@pytest.fixture
def sweep_module() -> ModuleType:
    return _load_sweep_module()


@pytest.fixture(autouse=True)
def _configure_logging_for_route_embedding() -> None:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    """Loading the sweep module exercises the same module-level import surface as
    `test_similarity_threshold_sweep_scoring.py`/`test_similarity_threshold_dataset.py`;
    configuring here keeps this test file's fixture behavior identical to theirs (root
    conftest's `_isolate_logging` already redirects `PS_LOGGING_DIR` to `tmp_path` and
    resets the facade per test).
    """
    configure_logging()


# --- AC-BI-001: loads the real curated-content corpus via parse_serialized_graph_json --


def test_loads_capability_nodes_via_parse_serialized_graph_json(
    sweep_module: ModuleType,
) -> None:
    nodes = sweep_module._load_curated_content_capability_nodes()

    assert len(nodes) == 778
    for node in nodes:
        assert node.label == "Capability"

    node_ids = {node.properties["id"] for node in nodes}
    for expected_id in (
        "cap_data_protection_impact_assessment_1e2cf0",
        "cap_risk_management_2cf786",
        "cap_conformity_assessment_a2edfc",
    ):
        assert expected_id in node_ids

    assert sweep_module.parse_serialized_graph_json is real_parse_serialized_graph_json


# --- AC-BI-003: missing/malformed/wrong-shaped baseline.json -> CuratedContentCorpusError --


def test_raises_curated_content_corpus_error_when_a_baseline_json_is_missing(
    sweep_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "GDPR-1.0").mkdir()
    (tmp_path / "GDPR-1.0" / "baseline.json").write_text(
        '{"nodes": [], "edges": []}', encoding="utf-8"
    )
    (tmp_path / "NIS2-1.0").mkdir()
    (tmp_path / "NIS2-1.0" / "baseline.json").write_text(
        '{"nodes": [], "edges": []}', encoding="utf-8"
    )
    # No CRA-1.0 directory at all.

    monkeypatch.setattr(sweep_module, "_CURATED_CONTENT_DIR", tmp_path)

    with pytest.raises(sweep_module.CuratedContentCorpusError):
        sweep_module._load_curated_content_capability_nodes()


def test_raises_curated_content_corpus_error_when_a_baseline_json_is_malformed(
    sweep_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for instrument_dir in sweep_module._CURATED_CONTENT_INSTRUMENT_DIRS:
        (tmp_path / instrument_dir).mkdir()
        (tmp_path / instrument_dir / "baseline.json").write_text(
            '{"nodes": [], "edges": []}', encoding="utf-8"
        )
    malformed_path = tmp_path / "NIS2-1.0" / "baseline.json"
    malformed_path.write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(sweep_module, "_CURATED_CONTENT_DIR", tmp_path)

    with pytest.raises(sweep_module.CuratedContentCorpusError) as excinfo:
        sweep_module._load_curated_content_capability_nodes()

    assert str(malformed_path) in str(excinfo.value)


def test_raises_curated_content_corpus_error_when_a_baseline_json_has_the_wrong_shape(
    sweep_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for instrument_dir in sweep_module._CURATED_CONTENT_INSTRUMENT_DIRS:
        (tmp_path / instrument_dir).mkdir()
        (tmp_path / instrument_dir / "baseline.json").write_text(
            '{"nodes": [], "edges": []}', encoding="utf-8"
        )
    wrong_shape_path = tmp_path / "CRA-1.0" / "baseline.json"
    wrong_shape_path.write_text('{"nodes": []}', encoding="utf-8")  # missing "edges"

    monkeypatch.setattr(sweep_module, "_CURATED_CONTENT_DIR", tmp_path)

    with pytest.raises(sweep_module.CuratedContentCorpusError):
        sweep_module._load_curated_content_capability_nodes()
