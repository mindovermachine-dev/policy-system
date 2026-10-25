"""Live wiring proof for `tools/domain-mapper/exclusion_audit.py`.

`@pytest.mark.falkordb_live @pytest.mark.llm_live`: each test reads one real native graph
(`cra_native`, `gdpr_native`, `nis2_native`, ingested fresh from Cellar by
`live_native_graphs`) through the real `CellarEliDomainMappingAdapter`, selects the real
`select_sample_units` sample (Tier A: the first 30 units; Tier B: up to a per-regulation cap of
regex-flagged conditional-permissive units), and runs the real `capture_unit_record` (which
calls `ps_service.domain_mapper.extraction._extract_candidates_for_unit` -> `route_completion`
against real Azure OpenAI) on every sampled unit. Any flagged (zero-candidate) unit is then run
through `propose_classification`, also a real LLM call but outside the extraction-call ceiling.

Every test is self-contained: it takes its own fresh sample, holds its own extraction-call
ceiling (the sample size), and writes its captured records only under its own `tmp_path`, then
reads them back with `load_regulation_records`, the loader the cached-mode CLI uses.

Read-only against FalkorDB by construction: `capture_unit_record`/`propose_classification`
only ever call `route_completion` (an LLM call) -- neither one, nor anything else in
`exclusion_audit.py`, issues a FalkorDB write. Each test additionally confirms that via a
`{regulation}_baseline` node-count Cypher query before and after the run (mirrors
`test_live_capstone.py`'s own "prove it against real FalkorDB state, not just in-memory return
values" posture).

Does NOT import the private `_native_citation_refs` helper from `test_live_capstone.py`;
`_native_citation_refs` below is this module's own local copy (`_fakes.py`'s "doubles stay
local to each test module" convention).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.falkordb_client import baseline_graph_name, native_graph_name
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.llm_interface.client import default_completion_caller

if TYPE_CHECKING:
    from types import ModuleType

    from litellm.types.utils import ModelResponse

    from domain_mapper._fakes import MakeEmitter
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.falkordb_client import GraphHandle

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_live_under_test"

_TIER_A_SIZE = 30
_CRA_TIER_B_SIZE = 10
_GDPR_NIS2_TIER_B_SIZE = 8

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)` --
# mirrors `test_live_capstone.py`'s exact pattern, for the same reason: this live test's
# whole point is to use the real configured model, so it must be read before that
# fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


def _only_instrument_id(native_graph: GraphHandle) -> str:
    """The id of the single `RegulatoryInstrument` these live graphs hold (one version each)."""
    rows = cast(
        "list[list[object]]",
        native_graph.query("MATCH (r:RegulatoryInstrument) RETURN r.id").result_set,
    )
    assert len(rows) == 1, f"expected exactly one RegulatoryInstrument, got {len(rows)}"
    return cast("str", rows[0][0])


def _load_exclusion_audit_module() -> ModuleType:
    """Load `exclusion_audit.py` by path -- same non-package-script-by-path pattern the
    sibling `test_exclusion_audit_*.py` files already establish (`tools/domain-mapper/`
    is hyphenated, not an importable dotted package).
    """
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


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _native_citation_refs(native_graph: GraphHandle) -> set[str]:
    """Local copy of `test_live_capstone.py::_native_citation_refs` (the private helper
    is not imported from that module; doubles/checks stay local per
    test module per `_fakes.py`'s documented convention).
    """
    article_refs = _query_rows(native_graph, "MATCH (a:ARTICLE) RETURN a.citation_ref")
    paragraph_refs = _query_rows(native_graph, "MATCH (p:PARAGRAPH) RETURN p.citation_ref")
    # The adapter also reads ANNEX nodes (the annex extension), whose units carry the
    # annex's own `citation_ref` ("Annex II"), so the provenance set must include them.
    annex_refs = _query_rows(native_graph, "MATCH (a:ANNEX) RETURN a.citation_ref")
    return (
        {cast("str", row[0]) for row in article_refs}
        | {cast("str", row[0]) for row in paragraph_refs}
        | {cast("str", row[0]) for row in annex_refs}
    )


def _node_count(graph: GraphHandle) -> int:
    return cast("int", _query_rows(graph, "MATCH (n) RETURN count(n)")[0][0])


class _CountingCompletionCaller:
    """Wraps the real `default_completion_caller`, counting every live extraction call and
    hard-stopping at `call_limit`. Never used for classification calls (those are outside
    the ceiling).
    """

    def __init__(self, *, call_limit: int) -> None:
        self.call_count = 0
        self._call_limit = call_limit

    def __call__(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        self.call_count += 1
        assert self.call_count <= self._call_limit, (
            f"live extraction call ceiling exceeded: {self.call_count} > {self._call_limit}"
        )
        return default_completion_caller(model=model, messages=messages, timeout=timeout)


def _run_fresh_regulation_sample(
    *,
    regulation: str,
    tier_b_size: int,
    records_path: Path,
    make_emitter: MakeEmitter,
    config: ServiceConfig,
) -> None:
    """A fresh Tier A + Tier B live sample for one regulation, capped at one extraction call
    per sampled unit (`_TIER_A_SIZE + tier_b_size` at most), with records written to
    `records_path` and read back through `load_regulation_records`.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL
    module = _load_exclusion_audit_module()

    db = connect_from_config(config)
    native_graph = select_graph(db, native_graph_name(regulation))
    baseline_graph = select_graph(db, baseline_graph_name(regulation))

    native_refs = _native_citation_refs(native_graph)
    baseline_count_before = _node_count(baseline_graph)

    adapter = CellarEliDomainMappingAdapter()
    all_units = adapter.read_native_units(native_graph, _only_instrument_id(native_graph))

    sampled = module.select_sample_units(
        all_units, tier_a_size=_TIER_A_SIZE, tier_b_size=tier_b_size
    )
    tier_a = [s for s in sampled if s.selection_tier == "A"]
    tier_b = [s for s in sampled if s.selection_tier == "B"]
    assert len(tier_a) == _TIER_A_SIZE
    assert len(tier_b) <= tier_b_size

    emitter, _log_path = make_emitter()
    extraction_caller = _CountingCompletionCaller(call_limit=_TIER_A_SIZE + tier_b_size)

    captured: list[dict[str, object]] = []
    for sampled_unit in sampled:
        unit = sampled_unit.unit
        record = module.capture_unit_record(
            unit,
            regulation=regulation,
            model=model,
            call_completion=extraction_caller,
            emitter=emitter,
        )
        assert record.citation_ref in native_refs, (
            f"{record.citation_ref!r} not found in {regulation.lower()}_native's own "
            "ARTICLE/PARAGRAPH/ANNEX citation_ref set -- provenance broken"
        )

        proposal = None
        if record.is_flagged:
            # A real classification call, outside the extraction-call ceiling.
            proposal = module.propose_classification(
                record.citation_ref,
                unit.text,
                model=model,
                call_completion=None,
                emitter=emitter,
            )
            assert proposal.source == "ai_proposed"

        row = module.render_report_row(record, proposal)
        assert row.startswith("|")
        assert row.endswith("|")
        assert record.citation_ref in row

        captured.append(
            {
                **dataclasses.asdict(record),
                "unit_text": unit.text,
                "article_heading": unit.article_heading,
                "classification": dataclasses.asdict(proposal) if proposal is not None else None,
                "selection_tier": sampled_unit.selection_tier,
                "tier_b_match_text": sampled_unit.tier_b_match_text,
            }
        )

    assert extraction_caller.call_count == len(sampled), (
        f"expected exactly {len(sampled)} live extraction calls for {regulation}, "
        f"got {extraction_caller.call_count}"
    )

    baseline_count_after = _node_count(baseline_graph)
    assert baseline_count_after == baseline_count_before, (
        f"{regulation.lower()}_baseline node count changed during this audit run -- "
        f"exclusion_audit.py must be read-only against {regulation.lower()}_native only "
        f"(before={baseline_count_before}, after={baseline_count_after})"
    )

    records_path.write_text(
        json.dumps({"regulation": regulation, "records": captured}, indent=2),
        encoding="utf-8",
    )
    loaded_records, _proposals = module.load_regulation_records(records_path)
    assert [loaded.citation_ref for loaded in loaded_records] == [
        s.unit.citation_ref for s in sampled
    ]


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.cellar_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_cra_sample(
    make_emitter: MakeEmitter, live_native_graphs: ServiceConfig, tmp_path: Path
) -> None:
    """Fresh Tier A (30) + Tier B (<=10) CRA sample against the real `cra_native` graph."""
    _run_fresh_regulation_sample(
        regulation="CRA",
        tier_b_size=_CRA_TIER_B_SIZE,
        records_path=tmp_path / "cra.json",
        make_emitter=make_emitter,
        config=live_native_graphs,
    )


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.cellar_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_gdpr_sample(
    make_emitter: MakeEmitter, live_native_graphs: ServiceConfig, tmp_path: Path
) -> None:
    """Fresh Tier A (30) + Tier B (<=8) GDPR sample against the real `gdpr_native` graph."""
    _run_fresh_regulation_sample(
        regulation="GDPR",
        tier_b_size=_GDPR_NIS2_TIER_B_SIZE,
        records_path=tmp_path / "gdpr.json",
        make_emitter=make_emitter,
        config=live_native_graphs,
    )


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.cellar_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_nis2_sample(
    make_emitter: MakeEmitter, live_native_graphs: ServiceConfig, tmp_path: Path
) -> None:
    """Fresh Tier A (30) + Tier B (<=8) NIS2 sample against the real `nis2_native` graph."""
    _run_fresh_regulation_sample(
        regulation="NIS2",
        tier_b_size=_GDPR_NIS2_TIER_B_SIZE,
        records_path=tmp_path / "nis2.json",
        make_emitter=make_emitter,
        config=live_native_graphs,
    )
