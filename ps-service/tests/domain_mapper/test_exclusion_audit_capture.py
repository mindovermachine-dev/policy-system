"""Tests for `tools/domain-mapper/exclusion_audit.py`'s capture stage (issue #27, Slice 1).

Slice 1 is hermetic end-to-end at tiny scope (PLAN.md §3): a hand-written structural fake
`CompletionCaller` (mirrors `test_extraction.py`'s established style — never
`unittest.mock`), 3 hand-built `ExtractionUnit`s (one scripted to "extract" 0 candidates,
one >0, one that raises `DomainMapperExtractionError` to prove isolation).

Loads `exclusion_audit.py` by path via `importlib.util.spec_from_file_location`, exactly
as `test_similarity_threshold_sweep_scoring.py` documents doing for
`tools/company-merge/company_merge_similarity_sweep.py` (same non-package-script-by-path
pattern, same `sys.modules` pre-registration) — `tools/domain-mapper/` is hyphenated, not
an importable dotted package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from types import ModuleType

    from domain_mapper._fakes import MakeEmitter

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_capture_under_test"


def _load_exclusion_audit_module() -> ModuleType:
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


# --- 3 fixture units (PLAN.md §3 Slice 1) ----------------------------------

_UNIT_ZERO_CANDIDATES = ExtractionUnit(
    citation_ref="CRA-1.0 Art. 1(1)",
    text="This Regulation applies to products with digital elements made available on the market.",
    article_number="1",
    paragraph_number="1",
    article_heading="Subject matter and scope",
)
_UNIT_ONE_CANDIDATE = ExtractionUnit(
    citation_ref="CRA-1.0 Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)
_UNIT_MALFORMED_RESPONSE = ExtractionUnit(
    citation_ref="CRA-1.0 Art. 99(9)",
    text="The manufacturer shall report actively exploited vulnerabilities without undue delay.",
    article_number="99",
    paragraph_number="9",
    article_heading="Reporting obligations",
)


def _model_response(content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def _scripted_call_completion(responses: dict[str, str]):
    """A `CompletionCaller` fake keyed by the unit's `citation_ref`.

    Mirrors `test_extraction.py::_scripted_call_completion` exactly — the citation_ref is
    always embedded in the user message as `"Citation: {citation_ref}"`.
    """

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        user_content = messages[1]["content"]
        for citation_ref, response in responses.items():
            if f"Citation: {citation_ref}" in user_content:
                return _model_response(response)
        raise AssertionError(f"no scripted response for message: {user_content!r}")

    return _call


def _zero_candidates_json() -> str:
    return json.dumps({"requirements": []})


def _one_candidate_json() -> str:
    return json.dumps(
        {
            "requirements": [
                {
                    "role_name": "Manufacturer",
                    "text": "Conduct a cybersecurity risk assessment.",
                    "type": "requirement",
                    "letter_suffix": None,
                    "confidence": 0.92,
                }
            ]
        }
    )


def test_capture_unit_record_shape_for_nonzero_candidate_unit(make_emitter: MakeEmitter) -> None:
    """AC-BI-004: citation_ref, article/paragraph numbers, and candidate_count are captured
    verbatim from the unit and the real (scripted) LLM response.
    """
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    call_completion = _scripted_call_completion(
        {_UNIT_ONE_CANDIDATE.citation_ref: _one_candidate_json()}
    )

    record = module.capture_unit_record(
        _UNIT_ONE_CANDIDATE,
        regulation="CRA",
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert record.regulation == "CRA"
    assert record.citation_ref == _UNIT_ONE_CANDIDATE.citation_ref
    assert record.article_number == _UNIT_ONE_CANDIDATE.article_number
    assert record.paragraph_number == _UNIT_ONE_CANDIDATE.paragraph_number
    assert record.candidate_count == 1
    assert record.status == "ok"


def test_zero_candidate_unit_is_flagged(make_emitter: MakeEmitter) -> None:
    """AC-BI-005: a unit whose real extraction call produced zero candidates is flagged."""
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    call_completion = _scripted_call_completion(
        {_UNIT_ZERO_CANDIDATES.citation_ref: _zero_candidates_json()}
    )

    record = module.capture_unit_record(
        _UNIT_ZERO_CANDIDATES,
        regulation="CRA",
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert record.candidate_count == 0
    assert record.status == "ok"
    assert record.is_flagged is True


def test_nonzero_candidate_unit_is_not_flagged(make_emitter: MakeEmitter) -> None:
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    call_completion = _scripted_call_completion(
        {_UNIT_ONE_CANDIDATE.citation_ref: _one_candidate_json()}
    )

    record = module.capture_unit_record(
        _UNIT_ONE_CANDIDATE,
        regulation="CRA",
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert record.is_flagged is False


def test_malformed_response_unit_is_captured_as_error_not_flagged(
    make_emitter: MakeEmitter,
) -> None:
    """Per-unit isolation mirrors `extraction._extract_all_candidates`: a malformed LLM
    response is captured as `status="error"`, never conflated with a genuine
    zero-candidate flag (`is_flagged` stays `False` — there is no "why zero" question to
    ask about a unit whose call itself failed).
    """
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    call_completion = _scripted_call_completion(
        {_UNIT_MALFORMED_RESPONSE.citation_ref: "{not valid json"}
    )

    record = module.capture_unit_record(
        _UNIT_MALFORMED_RESPONSE,
        regulation="CRA",
        model="fake-model",
        call_completion=call_completion,
        emitter=emitter,
    )

    assert record.status == "error"
    assert record.candidate_count == 0
    assert record.is_flagged is False
    assert record.citation_ref == _UNIT_MALFORMED_RESPONSE.citation_ref
