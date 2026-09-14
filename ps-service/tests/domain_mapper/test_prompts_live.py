"""AC-BI-008 live test (GH #25 Slice 8): the extraction prompt's multi-duty.

"trailing letters" mechanism against CRA Annex I's real, long, itemized
text -- not a fabricated/short fixture string.

`@pytest.mark.llm_live @pytest.mark.falkordb_live`: reads #14's own
live-populated `cra_native` graph through the now-ANNEX-extended
`CellarEliDomainMappingAdapter` (GH #25 Slices 1-7), builds the exact
system+user message pair `extraction.py::_build_extraction_messages` would
for the "Annex I" unit, calls `route_completion` for real against the
configured LLM Provider, and parses the response via
`parse_extraction_response`.

This is a verification-only slice (PLAN.md decision 1.8 / Slice 8): no
production code changes here. Per PLAN.md's own exit criterion, if this
assertion fails when run with real credentials, a prompt-variant follow-up
is a NEW slice for a later implementation pass -- not silently patched in
here.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from ps_service.config import load_config
from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.extraction import (
    _build_extraction_messages,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
)
from ps_service.domain_mapper.falkordb_client import (
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.domain_mapper.prompts import parse_extraction_response
from ps_service.llm_interface.completion import route_completion

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter

pytestmark = [pytest.mark.llm_live, pytest.mark.falkordb_live]

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)`
# for every test (that guard exists to keep a leaked `.env` value out of unrelated
# tests) -- mirrors `test_route_completion_live_provider.py`'s and
# `test_live_capstone.py`'s established pattern exactly, for the same reason: this
# live test's whole point is to use the real configured model, so it must be read
# before that fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE, "
    "PS_FALKORDB_HOST, PS_FALKORDB_PORT)",
)
def test_extraction_prompt_yields_multiple_duties_from_real_cra_annex_i(
    make_emitter: MakeEmitter,
) -> None:
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    # load_config() still resolves falkordb_host/port correctly here -- only
    # PS_LLMINTERFACE_MODEL/PS_LLMINTERFACE_EMBED_MODEL are stripped by the
    # autouse fixture, not PS_FALKORDB_HOST/PORT (mirrors test_live_capstone.py).
    config = load_config()
    db = connect_from_config(config)
    native_graph = select_graph(db, native_graph_name("CRA"))

    adapter = CellarEliDomainMappingAdapter()
    units = adapter.read_native_units(native_graph)

    annex_i_units = [unit for unit in units if unit.citation_ref == "Annex I"]
    assert len(annex_i_units) == 1, (
        f"expected exactly one 'Annex I' unit from CRA's native graph, "
        f"got {len(annex_i_units)}: {[u.citation_ref for u in units]}"
    )
    annex_i_unit = annex_i_units[0]

    messages = _build_extraction_messages(annex_i_unit)

    emitter, _log_path = make_emitter(filename="prompts_live.jsonl")
    result = route_completion(messages, model=model, call_completion=None, emitter=emitter)

    try:
        candidates = parse_extraction_response(result.text, annex_i_unit)
    except DomainMapperExtractionError as exc:
        pytest.fail(
            f"extraction response for CRA Annex I was not parseable: {exc}. "
            "Per PLAN.md Slice 8's exit criterion, do not patch the prompt here -- "
            "open a follow-up plan item for a prompt variant instead."
        )

    distinct_letter_suffixes = {
        candidate.letter_suffix for candidate in candidates if candidate.letter_suffix
    }
    assert len(distinct_letter_suffixes) > 1, (
        "expected the extraction prompt's multi-duty 'trailing letters' mechanism to "
        f"fire on CRA Annex I's real text (more than one distinct letter_suffix), got "
        f"{len(candidates)} candidate(s) with letter_suffixes "
        f"{[c.letter_suffix for c in candidates]!r}. Per PLAN.md Slice 8's exit "
        "criterion: STOP here, do not patch the prompt in this slice -- open a "
        "follow-up plan item for a prompt variant instead."
    )
