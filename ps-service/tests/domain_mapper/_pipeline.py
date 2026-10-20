"""Extraction and derivation driven end to end against a `BaselineRig`.

The real `extract_roles_and_requirements` and `derive_obligations_and_capabilities` run over the
rig (the gateway with in-memory log and graph fakes). Only the LLM is scripted, and the native
graph and the Domain Mapping adapter (the sources of the extraction units) are fixed. Every call
of `scripted_extraction()` / `scripted_derivation()` returns a fresh script with the same
answers, so a second rig can run the same pipeline and be compared digest for digest.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from litellm.types.utils import Choices, Message, ModelResponse

from domain_mapper._gateway_fakes import GatewayKeywords  # noqa: TC001 -- runtime annotation
from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import CompletionCaller
    from ps_service.logging import LogEmitter

INSTRUMENT_ID = "CRA-1.0"
UNIT_MANUFACTURER = ExtractionUnit(
    citation_ref="Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)
UNIT_IMPORTER = ExtractionUnit(
    citation_ref="Art. 14(2)",
    text="The importer shall verify the manufacturer's conformity assessment.",
    article_number="14",
    paragraph_number="2",
    article_heading="Obligations of importers",
)
OBLIGATION_TEXTS = ("Conduct Cybersecurity Risk Assessment", "Verify Conformity Assessment")
CAPABILITY_NAMES = ("Risk Assessment Tooling", "Conformity Verification System")


class _Result:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


class _InstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _NativeGraph:
    """Answers the instrument read with a fixed node; any other query is not expected."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        return _Result([[_InstrumentNode({"id": INSTRUMENT_ID, "title": "Cyber Resilience Act"})]])


class _Adapter:
    """Hands extraction two fixed units."""

    def read_native_units(
        self, graph: GraphHandle, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        return (UNIT_MANUFACTURER, UNIT_IMPORTER)


def _response(content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def _requirements(role_name: str, text: str) -> str:
    return json.dumps(
        {
            "requirements": [
                {
                    "role_name": role_name,
                    "role_is_actor": True,
                    "text": text,
                    "type": "requirement",
                    "letter_suffix": None,
                    "confidence": 0.9,
                }
            ]
        }
    )


def scripted_extraction() -> CompletionCaller:
    """A fresh extraction script: one answer per unit, keyed by the unit's citation."""
    answers = {
        UNIT_MANUFACTURER.citation_ref: _requirements(
            "Manufacturer", "Conduct a cybersecurity risk assessment."
        ),
        UNIT_IMPORTER.citation_ref: _requirements(
            "Importer", "Verify the manufacturer's conformity assessment."
        ),
    }

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        user_content = messages[1]["content"]
        for citation_ref, answer in answers.items():
            if f"Citation: {citation_ref}" in user_content:
                return _response(answer)
        raise AssertionError(f"no scripted response for message: {user_content!r}")

    return _call


def scripted_derivation() -> CompletionCaller:
    """A fresh derivation script: two Obligation mints, then two Capability mints, in order."""
    remaining = [
        *(
            json.dumps(
                {
                    "matched_existing_id": None,
                    "new_text": text,
                    "unmatchable": False,
                    "confidence": 0.9,
                }
            )
            for text in OBLIGATION_TEXTS
        ),
        *(
            json.dumps(
                {
                    "capabilities": [
                        {
                            "matched_existing_id": None,
                            "new_name": name,
                            "new_description": None,
                            "confidence": 0.9,
                        }
                    ]
                }
            )
            for name in CAPABILITY_NAMES
        ),
    ]

    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        if not remaining:
            raise AssertionError("no more scripted responses -- unexpected extra LLM call")
        return _response(remaining.pop(0))

    return _call


def run_extraction(
    rig: BaselineRig, emitter: LogEmitter, *, submitter: GroupSubmitter | None = None
) -> ExtractionResult:
    """Run the real extraction stage against `rig`, submitting through `submitter` if given."""
    keywords: GatewayKeywords = rig.gateway_keywords()
    if submitter is not None:
        keywords["submitter"] = submitter
    return extract_roles_and_requirements(
        INSTRUMENT_ID,
        adapter=_Adapter(),
        native_graph=_NativeGraph(),
        **keywords,
        model="fake-model",
        call_completion=scripted_extraction(),
        emitter=emitter,
    )


def run_derivation(rig: BaselineRig, emitter: LogEmitter) -> DerivationResult:
    """Run the real derivation stage against `rig` (it reads what extraction left)."""
    return derive_obligations_and_capabilities(
        INSTRUMENT_ID,
        **rig.stage_kwargs(),
        model="fake-model",
        call_completion=scripted_derivation(),
        emitter=emitter,
    )
