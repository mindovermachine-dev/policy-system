"""Tests for `tools/domain-mapper/exclusion_audit.py`'s classification-proposal stage
(issue #27, Slice 1, AC-BI-006/007 — AI-proposed only, per CONTEXT.md user decision 2).

Loads `exclusion_audit.py` by path exactly as `test_exclusion_audit_capture.py` does (own
`_MODULE_NAME` so `sys.modules` keys never collide across this tool's own test files run
together in one session).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Choices, Message, ModelResponse

if TYPE_CHECKING:
    from types import ModuleType

    from domain_mapper._fakes import MakeEmitter

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_classification_under_test"

_CITATION_REF = "CRA-1.0 Art. 1"
_RECORD_TEXT = "This Regulation applies to products with digital elements."


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


def _fake_call_completion(content: str):
    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return _model_response(content)

    return _call


def test_propose_classification_returns_scripted_label_and_rationale(
    make_emitter: MakeEmitter,
) -> None:
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    scripted = json.dumps(
        {
            "label": "correct_exclusion_scope_applicability",
            "rationale": "Describes the Regulation's own scope, not a duty on an actor.",
        }
    )

    proposal = module.propose_classification(
        _CITATION_REF,
        _RECORD_TEXT,
        model="fake-model",
        call_completion=_fake_call_completion(scripted),
        emitter=emitter,
    )

    assert proposal.citation_ref == _CITATION_REF
    assert proposal.label == "correct_exclusion_scope_applicability"
    assert proposal.rationale == "Describes the Regulation's own scope, not a duty on an actor."


def test_propose_classification_source_is_always_ai_proposed(make_emitter: MakeEmitter) -> None:
    """CHANGES.md row 4: `source` is a fixed `"ai_proposed"` value — no other source exists
    yet for this run (CONTEXT.md user decision 2).
    """
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    scripted = json.dumps({"label": "miss", "rationale": "States an operative shall-duty."})

    proposal = module.propose_classification(
        _CITATION_REF,
        _RECORD_TEXT,
        model="fake-model",
        call_completion=_fake_call_completion(scripted),
        emitter=emitter,
    )

    assert proposal.source == "ai_proposed"


def test_miss_classification_carries_rationale(make_emitter: MakeEmitter) -> None:
    """AC-BI-007: a "miss" classification records citation_ref + a one-line rationale."""
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    scripted = json.dumps(
        {"label": "miss", "rationale": "States an operative shall-duty that was not extracted."}
    )

    proposal = module.propose_classification(
        _CITATION_REF,
        _RECORD_TEXT,
        model="fake-model",
        call_completion=_fake_call_completion(scripted),
        emitter=emitter,
    )

    assert proposal.label == "miss"
    assert proposal.citation_ref == _CITATION_REF
    assert proposal.rationale


def test_propose_classification_raises_on_malformed_json(make_emitter: MakeEmitter) -> None:
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()

    with pytest.raises(Exception, match="not valid JSON") as exc_info:
        module.propose_classification(
            _CITATION_REF,
            _RECORD_TEXT,
            model="fake-model",
            call_completion=_fake_call_completion("{not valid json"),
            emitter=emitter,
        )
    assert isinstance(exc_info.value, module.ExclusionAuditError)


def test_propose_classification_raises_on_unrecognized_label(make_emitter: MakeEmitter) -> None:
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()
    scripted = json.dumps({"label": "not_a_real_label", "rationale": "irrelevant"})

    with pytest.raises(Exception) as exc_info:
        module.propose_classification(
            _CITATION_REF,
            _RECORD_TEXT,
            model="fake-model",
            call_completion=_fake_call_completion(scripted),
            emitter=emitter,
        )
    assert isinstance(exc_info.value, module.ExclusionAuditError)
    assert _CITATION_REF in str(exc_info.value)
