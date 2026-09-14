"""Live wiring proof for `tools/domain-mapper/exclusion_audit.py` (issue #27, Slice 2,
PLAN.md §3 "Slice 2 -- Live, tiny scope, one regulation (CRA)", adjusted per CHANGES.md
rows 1-3 and 6).

`@pytest.mark.falkordb_live @pytest.mark.llm_live`: reads the real, already-populated
`cra_native` FalkorDB graph through the real `CellarEliDomainMappingAdapter`, then runs
the real `capture_unit_record` (which calls
`ps_service.domain_mapper.extraction._extract_candidates_for_unit` ->
`route_completion` against real Azure OpenAI) on exactly the first 5 CRA
`ExtractionUnit`s in document order -- no more (CHANGES.md row 1: this slice's own
budget is exactly 5 live extraction calls; a running counter also hard-stops at the
cumulative <=120 ceiling shared across Slices 2-4). Any flagged (zero-candidate) unit
is then run through `propose_classification` (also a real, but separately-budgeted,
LLM call -- CONTEXT.md decision 5 excludes classification calls from the 120-call
extraction ceiling).

Read-only against FalkorDB by construction: `capture_unit_record`/`propose_classification`
only ever call `route_completion` (an LLM call) -- neither one, nor anything else in
`exclusion_audit.py`, issues a FalkorDB write. This test additionally confirms that
belt-and-suspenders, via a `cra_baseline` node-count Cypher query before and after the
run (mirrors `test_live_capstone.py`'s own "prove it against real FalkorDB state, not
just in-memory return values" posture).

CHANGES.md row 3: does NOT import the private `_native_citation_refs` helper from
`test_live_capstone.py` -- `_native_citation_refs` below is this module's own local copy
(`_fakes.py`'s "doubles stay local to each test module" convention).

CHANGES.md row 6 observation, stated after actually running this test live (not assumed
-- an earlier draft of this paragraph guessed "3 paragraphs of Art. 1 + 2 of Art. 2" and
was WRONG; corrected against the real captured `records/cra.json` output below): the
first 5 CRA `ExtractionUnit`s in document order are Art. 1 (a single, paragraph-less
unit, heading "Subject matter") and Art. 2(1)-2(4) (four paragraphs, heading "Scope").
4 of the 5 came back `status="ok"`, `candidate_count=0` (flagged) and were AI-classified
`correct_exclusion_scope_applicability` with rationales matching their text (Art. 1 lists
what the Regulation "lays down"; Art. 2(1) states what it "applies to"; Art. 2(3)/2(4)
state what it "does not apply to"). The 5th, Art. 2(2) (another "does not apply to" list),
came back `status="error"` (a malformed/unparseable LLM response for that one call) --
captured as an error, not conflated with a zero-candidate flag, and correctly never sent
to classification. So: of the 5, 4/4 parseable results were genuine scope/applicability
text (no misses), and 1/5 was a capture-layer parsing hiccup unrelated to the
exclusion-vs-miss question. This is a real, if small, confirmation of PLAN.md §1's
"scope/definitions cluster at the start" hypothesis for the units actually sampled here
-- note Art. 1 and 2 both landed on "scope", not "definitions" (Art. 3, "Definitions",
falls just outside this 5-unit window) -- Slice 3's wider Tier A run (30 units) is needed
before treating the hypothesis as confirmed for CRA as a whole.

**Slice 3** (PLAN.md §3 "Slice 3 -- Widen sampling to the real Tier A + Tier B strategy,
CRA only", CHANGES.md row 1a) adds `test_live_audit_extends_cra_sample_to_tier_a_and_b`:
loads this file's own cached `records/cra.json` (Slice 2's 5 units), computes the real
`select_sample_units` sample (Tier A first 30 + Tier B up to 10) over the FULL real
`cra_native` unit list, calls `capture_unit_record` only for units not already cached (up
to 35 net-new calls -- 25 remaining Tier A + up to 10 Tier B), and writes the merged
~40-entry result back to `records/cra.json`. `_CountingCompletionCaller` below is
generalized (an optional `start_count`/`slice_limit` pair, defaulting to Slice 2's own
values) so this slice's counter starts at Slice 2's actual 5 calls and enforces both the
120-call cumulative ceiling and this slice's own <=35 net-new ceiling, rather than
reinventing the counting pattern (CHANGES.md row 1's "carry Slice 2's actual call count
forward as the starting point, not reset to 0").

CHANGES.md row 6 observation continued, stated after actually running Slice 3's wider
Tier A window live: see this module's `test_live_audit_extends_cra_sample_to_tier_a_and_b`
docstring below for the real, post-run finding on whether Art. 3 onward still reads as
scope/definitions or shifts to substantive duties.

CHANGES.md row 7 observation: see the same docstring below for whether any real Tier B
match in this run was the named false-positive shape ("may impose fines where...",
empowerment rather than the target passive "may be X where...").

**Slice 4** (PLAN.md §3 "Slice 4 -- All three regulations + the committed findings
report", tightened per the orchestrator's corrected Slice 4 budget, not PLAN.md's
original 40/regulation): adds `test_live_audit_captures_gdpr_sample` and
`test_live_audit_captures_nis2_sample`, each a FRESH (no prior cache to merge with,
unlike CRA's Slice 3) Tier A (first 30 units) + Tier B (up to `_SLICE_4_TIER_B_SIZE = 8`
regex-flagged units) run against the real `gdpr_native`/`nis2_native` graphs, hard-capped
at `_SLICE_4_PER_REGULATION_CALL_LIMIT = 38` new extraction calls each (30 Tier A + <=8
Tier B <= 38, never 40) and writing `records/gdpr.json`/`records/nis2.json`.

The true cumulative extraction-call count going into this slice is **42**, not the 37
persisted in `records/cra.json` -- a prior slice's accidental test re-run spent 5 extra
real calls not reflected in that persisted cache count (orchestrator-corrected
accounting, CHANGES.md row 1's own ceiling arithmetic superseded for this run only).
`_CountingCompletionCaller` is seeded accordingly: the GDPR test starts its counter at
42 (cap 80), the NIS2 test starts at 80 -- the GDPR test's own worst-case ceiling,
whether or not GDPR's actual run used its full 38-call budget -- so the two tests'
ceilings compose correctly regardless of run order or whether only one is selected
(cap 118), staying inside the shared 120-call budget with a 2-call safety margin.
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

from ps_service.config import load_config
from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.falkordb_client import (
    baseline_graph_name,
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.llm_interface.client import default_completion_caller

if TYPE_CHECKING:
    from types import ModuleType

    from litellm.types.utils import ModelResponse

    from domain_mapper._fakes import MakeEmitter
    from ps_service.domain_mapper.falkordb_client import GraphHandle

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_live_under_test"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECORDS_DIR = _REPO_ROOT / ".orchestrator" / "tracker" / "issue-27-exclusion-audit" / "records"
_CRA_RECORDS_PATH = _RECORDS_DIR / "cra.json"

_REGULATION = "CRA"
_LIVE_UNIT_LIMIT = 5  # CHANGES.md row 1: Slice 2's own hard scope, no more.
# CHANGES.md row 1: cumulative ceiling shared across Slices 2-4, zero margin.
_MAX_CUMULATIVE_EXTRACTION_CALLS = 120

# Slice 3 (CHANGES.md row 1a): additive over Slice 2's cached 5 CRA units. Tier A is the
# first 30 CRA units (5 already cached + 25 net-new) + Tier B up to 10 net-new -- up to 35
# net-new extraction calls this slice, carried forward from Slice 2's actual 5 so the
# cumulative CRA total stays <=40.
_SLICE_2_ACTUAL_CALL_COUNT = 5
_SLICE_3_NEW_CALL_LIMIT = 35

# Slice 4 (orchestrator-corrected budget, tightened from PLAN.md's original 40/regulation):
# GDPR and NIS2 each get a FRESH (no prior cache) Tier A (30) + Tier B (<=8) sample, hard-
# capped at 38 new extraction calls per regulation -- never 40. The true cumulative count
# going into this slice is 42 (not the 37 persisted in records/cra.json -- a prior slice's
# accidental test re-run spent 5 extra real calls not reflected there). Ceiling stays <=120
# with a 2-call safety margin: 42 + 38 (GDPR) + 38 (NIS2) = 118.
_TRUE_CUMULATIVE_BEFORE_SLICE_4 = 42
_SLICE_4_TIER_A_SIZE = 30
_SLICE_4_TIER_B_SIZE = 8
_SLICE_4_PER_REGULATION_CALL_LIMIT = 38
_GDPR_RECORDS_PATH = _RECORDS_DIR / "gdpr.json"
_NIS2_RECORDS_PATH = _RECORDS_DIR / "nis2.json"

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)` --
# mirrors `test_live_capstone.py`'s exact pattern, for the same reason: this live test's
# whole point is to use the real configured model, so it must be read before that
# fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


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
    """Local copy of `test_live_capstone.py::_native_citation_refs` (CHANGES.md row 3 --
    the private helper is not imported from that module; doubles/checks stay local per
    test module per `_fakes.py`'s documented convention).
    """
    article_refs = _query_rows(native_graph, "MATCH (a:ARTICLE) RETURN a.citation_ref")
    paragraph_refs = _query_rows(native_graph, "MATCH (p:PARAGRAPH) RETURN p.citation_ref")
    return {cast("str", row[0]) for row in article_refs} | {
        cast("str", row[0]) for row in paragraph_refs
    }


def _node_count(graph: GraphHandle) -> int:
    return cast("int", _query_rows(graph, "MATCH (n) RETURN count(n)")[0][0])


def _load_cached_cra_records() -> dict[str, dict[str, object]]:
    """Load `records/cra.json` (if it exists) keyed by `citation_ref` -- the cache Slice 3
    is additive over (CHANGES.md row 1a). Returns `{}` if no cache file exists yet.
    """
    if not _CRA_RECORDS_PATH.exists():
        return {}
    payload = json.loads(_CRA_RECORDS_PATH.read_text(encoding="utf-8"))
    records = cast("list[dict[str, object]]", payload["records"])
    return {cast("str", record["citation_ref"]): record for record in records}


class _CountingCompletionCaller:
    """Wraps the real `default_completion_caller`, counting every live extraction call
    and hard-stopping both at the cumulative `_MAX_CUMULATIVE_EXTRACTION_CALLS` ceiling
    shared across Slices 2-4 and at this slice's own net-new-call budget (CHANGES.md row 1
    -- "add a running call-counter assertion... as a hard stop"). Never used for
    classification calls (CONTEXT.md decision 5: those are outside this ceiling).

    `start_count` seeds `call_count` at a prior slice's own actual cumulative total
    (CHANGES.md row 1: "carry Slice 2's actual call count forward as the starting point,
    not reset to 0") -- `call_count` is therefore the true cumulative total across the
    whole run, while `new_call_count` tracks only calls made through *this* instance
    (i.e. this slice's own net-new spend). Defaults (`start_count=0`,
    `slice_limit=_LIVE_UNIT_LIMIT`) reproduce Slice 2's original, unseeded behaviour
    exactly.
    """

    def __init__(self, *, start_count: int = 0, slice_limit: int = _LIVE_UNIT_LIMIT) -> None:
        self.call_count = start_count
        self.new_call_count = 0
        self._slice_limit = slice_limit

    def __call__(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        self.call_count += 1
        self.new_call_count += 1
        assert self.call_count <= _MAX_CUMULATIVE_EXTRACTION_CALLS, (
            f"live extraction call budget exceeded: {self.call_count} > "
            f"{_MAX_CUMULATIVE_EXTRACTION_CALLS} (CHANGES.md row 1 cumulative ceiling "
            "across Slices 2-4)"
        )
        assert self.new_call_count <= self._slice_limit, (
            f"this slice's own live extraction call budget exceeded: "
            f"{self.new_call_count} > {self._slice_limit}"
        )
        return default_completion_caller(model=model, messages=messages, timeout=timeout)


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_first_five_cra_units(make_emitter: MakeEmitter) -> None:
    """Runs the real capture -> flag -> propose(AI-labeled) -> report-row chain on the
    first 5 CRA `ExtractionUnit`s (document order) against the real `cra_native` graph
    and real Azure OpenAI, and writes the captured records to
    `.orchestrator/tracker/issue-27-exclusion-audit/records/cra.json` (CHANGES.md row 1b
    -- the cache Slice 3 will load and extend).
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    module = _load_exclusion_audit_module()

    # load_config() still resolves falkordb_host/port correctly here -- only
    # PS_LLMINTERFACE_MODEL/PS_LLMINTERFACE_EMBED_MODEL are stripped by the autouse
    # fixture, not PS_FALKORDB_HOST/PORT.
    config = load_config()
    db = connect_from_config(config)
    native_graph = select_graph(db, native_graph_name(_REGULATION))
    baseline_graph = select_graph(db, baseline_graph_name(_REGULATION))

    native_refs = _native_citation_refs(native_graph)
    baseline_count_before = _node_count(baseline_graph)

    adapter = CellarEliDomainMappingAdapter()
    all_units = adapter.read_native_units(native_graph)
    assert len(all_units) >= _LIVE_UNIT_LIMIT, (
        f"cra_native has only {len(all_units)} ExtractionUnits; need at least {_LIVE_UNIT_LIMIT}"
    )
    units = all_units[:_LIVE_UNIT_LIMIT]

    emitter, _log_path = make_emitter()
    extraction_caller = _CountingCompletionCaller()

    records = [
        module.capture_unit_record(
            unit,
            regulation=_REGULATION,
            model=model,
            call_completion=extraction_caller,
            emitter=emitter,
        )
        for unit in units
    ]

    assert extraction_caller.call_count == _LIVE_UNIT_LIMIT, (
        f"expected exactly {_LIVE_UNIT_LIMIT} live extraction calls, got "
        f"{extraction_caller.call_count}"
    )

    serializable_records: list[dict[str, object]] = []
    for unit, record in zip(units, records, strict=True):
        assert record.citation_ref in native_refs, (
            f"{record.citation_ref!r} not found in cra_native's own ARTICLE/PARAGRAPH "
            "citation_ref set -- provenance broken"
        )

        proposal = None
        if record.is_flagged:
            # A real, but separately-budgeted, classification call (CONTEXT.md decision
            # 5) -- not counted against `_MAX_CUMULATIVE_EXTRACTION_CALLS`.
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

        serializable_records.append(
            {
                **dataclasses.asdict(record),
                "unit_text": unit.text,
                "article_heading": unit.article_heading,
                "classification": dataclasses.asdict(proposal) if proposal is not None else None,
            }
        )

    baseline_count_after = _node_count(baseline_graph)
    assert baseline_count_after == baseline_count_before, (
        "cra_baseline node count changed during this audit run -- exclusion_audit.py "
        f"must be read-only against cra_native only (before={baseline_count_before}, "
        f"after={baseline_count_after})"
    )

    _RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    _CRA_RECORDS_PATH.write_text(
        json.dumps({"regulation": _REGULATION, "records": serializable_records}, indent=2),
        encoding="utf-8",
    )


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_extends_cra_sample_to_tier_a_and_b(make_emitter: MakeEmitter) -> None:
    """Slice 3 (PLAN.md §3, CHANGES.md row 1a): widens Slice 2's tiny 5-unit CRA sample to
    the real Tier A (first 30 units) + Tier B (up to 10 regex-flagged conditional-
    permissive units) strategy, additively over Slice 2's already-cached
    `records/cra.json` -- issues live calls ONLY for units not already cached.

    Asserts: net-new call count equals the net-new sample size and stays <=
    `_SLICE_3_NEW_CALL_LIMIT` (35); no cached unit is ever re-selected for a live call
    (cache-loading correctness); the cumulative counter (seeded at Slice 2's actual 5,
    CHANGES.md row 1) stays <=120; the final merged `records/cra.json` holds every cached
    unit plus every newly captured unit (37 total for CRA on the actual run -- Tier A's
    full 30 + only 7 real Tier B regex matches found in the rest of the document, short
    of the 10 cap).

    CHANGES.md row 6 observation, stated after actually running this live (not assumed):
    Art. 1-3 (10 Tier A units: Art. 1, Art. 2(1)-2(8), Art. 3) are 100% flagged
    (zero-candidate) -- 9 classified `correct_exclusion_scope_applicability`, 1
    (Art. 2(5), "the Regulation's application... may be limited") classified
    `correct_exclusion_conditional_permissive`. From Art. 4 onward (the remaining 20
    Tier A units, Art. 4(1)-12(3)), the flag rate drops sharply to 7/20 (35%) -- most
    units now produce real `RequirementCandidate`s. This is real, if still small-N,
    confirmation of PLAN.md §1's "scope/definitions cluster at the start" hypothesis: a
    hard 100%-vs-35% split right at the Art. 3/4 boundary. Notably, this run's only 2
    AI-proposed "miss" classifications (Art. 7(4), Art. 12(2) -- both genuine
    Commission/notified-body operative duties the prompt should have surfaced) occur
    exclusively in the post-Art.-3 window, never in the scope/definitions cluster --
    i.e. where misses actually appear (pending human sign-off, CONTEXT.md decision 2) is
    NOT where scope-exclusion zero-candidates cluster.

    CHANGES.md row 7 observation: of the 7 real Tier B regex matches, 6 (Art. 17(1),
    17(2), 27(7), 33(2), 43(5), 63(4)) are the ACTIVE "[actor] may [verb] ...
    where/if <condition>" empowerment/discretionary-power shape CHANGES.md row 7 named
    as a known false-positive risk (e.g. Art. 33(2) "Member States may, where
    appropriate, establish cyber resilience regulatory sandboxes" is structurally
    identical to the named counter-example "may impose fines where the infringement is
    severe") -- not the narrower passive "may be X where <conditions>" pattern PLAN.md
    §1's prose emphasized. Only 1 of 7 (Art. 13(4), "the cybersecurity risk assessment
    may [be]...") is closer to that passive shape. This counts AGAINST the regex's
    precision as literally described, confirming CHANGES.md row 7's concern is real.
    However, in THIS run it caused no harmful audit outcome: 5 of the 7 matched units
    were not even flagged (they had real extractable content elsewhere in the same
    unit, so the exclusion prompt never suppressed them); of the 2 that WERE flagged
    and AI-classified (Art. 17(1), Art. 63(4), both active-empowerment shape), both
    were judged legitimate `correct_exclusion_conditional_permissive`, zero misses
    among Tier B. Net signal for PLAN.md §1 Open Question 3: the regex is broader/
    noisier than its prose description (it catches active-voice empowerment grants, not
    only passive constructions) but not obviously harmful at this sample size --
    Tier-A-only fallback is not clearly warranted yet, though a human reviewer should
    look closely at Art. 17(1)/63(4)'s classifications given the shape mismatch.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    module = _load_exclusion_audit_module()

    config = load_config()
    db = connect_from_config(config)
    native_graph = select_graph(db, native_graph_name(_REGULATION))
    baseline_graph = select_graph(db, baseline_graph_name(_REGULATION))

    native_refs = _native_citation_refs(native_graph)
    baseline_count_before = _node_count(baseline_graph)

    adapter = CellarEliDomainMappingAdapter()
    all_units = adapter.read_native_units(native_graph)

    cached_by_ref = _load_cached_cra_records()
    assert cached_by_ref, (
        "expected Slice 2's cached records/cra.json to already exist -- this slice is "
        "additive, not a fresh run (CHANGES.md row 1a)"
    )

    sampled = module.select_sample_units(all_units)
    tier_a = [s for s in sampled if s.selection_tier == "A"]
    tier_b = [s for s in sampled if s.selection_tier == "B"]
    assert len(tier_a) == 30
    assert len(tier_b) <= 10

    # Sanity check on the "additive" premise itself: every unit Slice 2 already cached
    # must fall inside this run's own Tier A window (CHANGES.md row 1a assumes the first
    # 5 doc-order units are stable across slices).
    tier_a_refs = {s.unit.citation_ref for s in tier_a}
    assert set(cached_by_ref) <= tier_a_refs, (
        "Slice 2's cached units are not a subset of this run's Tier A window -- the "
        "'additive over Slice 2' assumption (CHANGES.md row 1a) does not hold"
    )

    new_sampled = [s for s in sampled if s.unit.citation_ref not in cached_by_ref]
    new_refs = {s.unit.citation_ref for s in new_sampled}
    assert new_refs.isdisjoint(cached_by_ref.keys()), (
        "cache-loading correctness: a cached unit was re-selected for a live call"
    )
    assert len(new_sampled) <= _SLICE_3_NEW_CALL_LIMIT, (
        f"net-new sample ({len(new_sampled)}) exceeds this slice's own "
        f"<={_SLICE_3_NEW_CALL_LIMIT}-call budget"
    )

    emitter, _log_path = make_emitter()
    extraction_caller = _CountingCompletionCaller(
        start_count=_SLICE_2_ACTUAL_CALL_COUNT, slice_limit=_SLICE_3_NEW_CALL_LIMIT
    )

    merged_by_ref: dict[str, dict[str, object]] = dict(cached_by_ref)

    for sampled_unit in new_sampled:
        unit = sampled_unit.unit
        record = module.capture_unit_record(
            unit,
            regulation=_REGULATION,
            model=model,
            call_completion=extraction_caller,
            emitter=emitter,
        )
        assert record.citation_ref in native_refs, (
            f"{record.citation_ref!r} not found in cra_native's own ARTICLE/PARAGRAPH "
            "citation_ref set -- provenance broken"
        )

        proposal = None
        if record.is_flagged:
            # A real, but separately-budgeted, classification call (CONTEXT.md decision
            # 5) -- not counted against `_MAX_CUMULATIVE_EXTRACTION_CALLS`.
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

        merged_by_ref[record.citation_ref] = {
            **dataclasses.asdict(record),
            "unit_text": unit.text,
            "article_heading": unit.article_heading,
            "classification": dataclasses.asdict(proposal) if proposal is not None else None,
            "selection_tier": sampled_unit.selection_tier,
            "tier_b_match_text": sampled_unit.tier_b_match_text,
        }

    assert extraction_caller.new_call_count == len(new_sampled), (
        f"expected exactly {len(new_sampled)} net-new live extraction calls, got "
        f"{extraction_caller.new_call_count}"
    )
    assert extraction_caller.new_call_count <= _SLICE_3_NEW_CALL_LIMIT
    expected_cumulative = _SLICE_2_ACTUAL_CALL_COUNT + extraction_caller.new_call_count
    assert extraction_caller.call_count == expected_cumulative
    assert extraction_caller.call_count <= 40, (
        f"cumulative CRA extraction call count ({extraction_caller.call_count}) exceeds "
        "this slice's <=40 CRA target (CHANGES.md row 1a)"
    )
    assert extraction_caller.call_count <= _MAX_CUMULATIVE_EXTRACTION_CALLS

    # Backfill selection_tier="A" for Slice 2's own cached entries (all within the first
    # 5 units, i.e. inside the Tier A window) so every entry in the merged file carries
    # the same schema going forward.
    for entry in merged_by_ref.values():
        if "selection_tier" not in entry:
            entry["selection_tier"] = "A"
            entry["tier_b_match_text"] = None

    baseline_count_after = _node_count(baseline_graph)
    assert baseline_count_after == baseline_count_before, (
        "cra_baseline node count changed during this audit run -- exclusion_audit.py "
        f"must be read-only against cra_native only (before={baseline_count_before}, "
        f"after={baseline_count_after})"
    )

    assert len(merged_by_ref) <= 40, f"expected ~40 CRA records total, got {len(merged_by_ref)}"

    # Preserve document order in the written file (native adapter order), not dict
    # insertion/merge order -- easier for a human reviewer to scan.
    doc_order = {unit.citation_ref: index for index, unit in enumerate(all_units)}
    ordered_records = sorted(
        merged_by_ref.values(),
        key=lambda record: doc_order.get(cast("str", record["citation_ref"]), len(all_units)),
    )

    _RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    _CRA_RECORDS_PATH.write_text(
        json.dumps({"regulation": _REGULATION, "records": ordered_records}, indent=2),
        encoding="utf-8",
    )


def _run_fresh_regulation_sample(
    *,
    module: ModuleType,
    regulation: str,
    model: str,
    records_path: Path,
    start_count: int,
    make_emitter: MakeEmitter,
) -> None:
    """Slice 4: a FRESH Tier A (30) + Tier B (<=8) live sample for one regulation with no
    prior cache to merge with (unlike CRA's Slice 3, which is additive over Slice 2's
    5-unit cache) -- GDPR and NIS2 are both first live-touched in this slice. Hard-capped
    at `_SLICE_4_PER_REGULATION_CALL_LIMIT` (38) new extraction calls via
    `_CountingCompletionCaller`, seeded at `start_count` (the true cumulative total going
    into this regulation's run, per this module's own docstring accounting) so the shared
    120-call ceiling is enforced across the whole run, not just this regulation's own
    38-call slice.
    """
    config = load_config()
    db = connect_from_config(config)
    native_graph = select_graph(db, native_graph_name(regulation))
    baseline_graph = select_graph(db, baseline_graph_name(regulation))

    native_refs = _native_citation_refs(native_graph)
    baseline_count_before = _node_count(baseline_graph)

    adapter = CellarEliDomainMappingAdapter()
    all_units = adapter.read_native_units(native_graph)

    sampled = module.select_sample_units(
        all_units, tier_a_size=_SLICE_4_TIER_A_SIZE, tier_b_size=_SLICE_4_TIER_B_SIZE
    )
    tier_a = [s for s in sampled if s.selection_tier == "A"]
    tier_b = [s for s in sampled if s.selection_tier == "B"]
    assert len(tier_a) == _SLICE_4_TIER_A_SIZE
    assert len(tier_b) <= _SLICE_4_TIER_B_SIZE
    assert len(sampled) <= _SLICE_4_PER_REGULATION_CALL_LIMIT, (
        f"{regulation} sample size ({len(sampled)}) exceeds the "
        f"<={_SLICE_4_PER_REGULATION_CALL_LIMIT}-call budget for this slice"
    )

    emitter, _log_path = make_emitter()
    extraction_caller = _CountingCompletionCaller(
        start_count=start_count, slice_limit=_SLICE_4_PER_REGULATION_CALL_LIMIT
    )

    captured: dict[str, dict[str, object]] = {}
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
            "ARTICLE/PARAGRAPH citation_ref set -- provenance broken"
        )

        proposal = None
        if record.is_flagged:
            # A real, but separately-budgeted, classification call (CONTEXT.md decision
            # 5) -- not counted against `_SLICE_4_PER_REGULATION_CALL_LIMIT` or the
            # cumulative 120-call ceiling.
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

        captured[record.citation_ref] = {
            **dataclasses.asdict(record),
            "unit_text": unit.text,
            "article_heading": unit.article_heading,
            "classification": dataclasses.asdict(proposal) if proposal is not None else None,
            "selection_tier": sampled_unit.selection_tier,
            "tier_b_match_text": sampled_unit.tier_b_match_text,
        }

    assert extraction_caller.new_call_count == len(sampled), (
        f"expected exactly {len(sampled)} net-new live extraction calls for {regulation}, "
        f"got {extraction_caller.new_call_count}"
    )
    assert extraction_caller.new_call_count <= _SLICE_4_PER_REGULATION_CALL_LIMIT
    assert extraction_caller.call_count == start_count + extraction_caller.new_call_count
    assert extraction_caller.call_count <= _MAX_CUMULATIVE_EXTRACTION_CALLS

    baseline_count_after = _node_count(baseline_graph)
    assert baseline_count_after == baseline_count_before, (
        f"{regulation.lower()}_baseline node count changed during this audit run -- "
        f"exclusion_audit.py must be read-only against {regulation.lower()}_native only "
        f"(before={baseline_count_before}, after={baseline_count_after})"
    )

    # Preserve document order in the written file (native adapter order), not dict
    # insertion/merge order -- easier for a human reviewer to scan.
    doc_order = {unit.citation_ref: index for index, unit in enumerate(all_units)}
    ordered_records = sorted(
        captured.values(),
        key=lambda record: doc_order.get(cast("str", record["citation_ref"]), len(all_units)),
    )

    _RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    records_path.write_text(
        json.dumps({"regulation": regulation, "records": ordered_records}, indent=2),
        encoding="utf-8",
    )


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_gdpr_sample(make_emitter: MakeEmitter) -> None:
    """Slice 4: fresh Tier A + Tier B GDPR sample against the real `gdpr_native` graph,
    <=38 new extraction calls, written to `records/gdpr.json`. Counter seeded at this
    module's own `_TRUE_CUMULATIVE_BEFORE_SLICE_4` (42) so the cumulative ceiling check
    reflects the orchestrator-corrected true call count, not just this test's own count.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    module = _load_exclusion_audit_module()
    _run_fresh_regulation_sample(
        module=module,
        regulation="GDPR",
        model=_LLM_INTERFACE_MODEL,
        records_path=_GDPR_RECORDS_PATH,
        start_count=_TRUE_CUMULATIVE_BEFORE_SLICE_4,
        make_emitter=make_emitter,
    )


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_audit_captures_nis2_sample(make_emitter: MakeEmitter) -> None:
    """Slice 4: fresh Tier A + Tier B NIS2 sample against the real `nis2_native` graph,
    <=38 new extraction calls, written to `records/nis2.json`. Counter seeded at
    `_TRUE_CUMULATIVE_BEFORE_SLICE_4 + _SLICE_4_PER_REGULATION_CALL_LIMIT` (80) -- the
    GDPR test's own worst-case ceiling -- so this test's cumulative-ceiling check holds
    regardless of run order or whether only one of the two Slice 4 live tests is selected.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    module = _load_exclusion_audit_module()
    _run_fresh_regulation_sample(
        module=module,
        regulation="NIS2",
        model=_LLM_INTERFACE_MODEL,
        records_path=_NIS2_RECORDS_PATH,
        start_count=_TRUE_CUMULATIVE_BEFORE_SLICE_4 + _SLICE_4_PER_REGULATION_CALL_LIMIT,
        make_emitter=make_emitter,
    )
