"""AC-BI-009 live test: RouteStructuredCompletion against the real, configured LLM Provider."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import pytest
from litellm.utils import supports_response_schema
from pydantic import BaseModel, Field

from ps_service.llm_interface.models import ChatMessage, StructuredCompletionResult
from ps_service.llm_interface.structured_completion import route_structured_completion
from ps_service.logging.emitter import EmitterConfig, LogEmitter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.llm_live

# Captured at module-import time (collection), before the autouse `_isolate_logging`
# fixture in `tests/conftest.py` runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)`
# for every test. Reading `os.environ["PS_LLMINTERFACE_MODEL"]` inside the test body
# itself would KeyError once that fixture has run — the guard exists precisely to
# keep leaked `.env` values out of unrelated tests, and this live test's whole point
# is to use the real value, so it must be captured before the fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


class _ExtractedObligation(BaseModel):
    statement: str
    severity: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0)


class _ExtractionReport(BaseModel):
    obligations: list[_ExtractedObligation]
    note: str | None


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """A real `LogEmitter` writing to a per-test tmp path — `route_structured_completion`'s.

    `log` call needs a live emitter (or a configured process default) or it raises
    `LoggingLifecycleError`; this test doesn't assert on log content, only that
    `route_structured_completion` itself behaves correctly, so a throwaway emitter
    is enough.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


_SNIPPET = (
    "Operators shall submit an annual compliance report by 31 March each year. "
    "Operators must retain supporting records for five years."
)


@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_route_structured_completion_returns_validated_instance_from_live_provider(
    emitter: LogEmitter,
) -> None:
    assert (
        _LLM_INTERFACE_MODEL is not None
    )  # narrows type for mypy/ruff; skipif already guards this
    result = route_structured_completion(
        [
            ChatMessage(
                role="user",
                content=(
                    "Extract each obligation from the following regulatory snippet. "
                    "For each obligation, give the statement, a severity of low, medium "
                    "or high, and a confidence between 0.0 and 1.0. Include a note only "
                    f"if relevant, otherwise leave it null.\n\n{_SNIPPET}"
                ),
            )
        ],
        model=_LLM_INTERFACE_MODEL,
        response_model=_ExtractionReport,
        call_completion=None,
        emitter=emitter,
    )

    assert isinstance(result, StructuredCompletionResult)
    assert isinstance(result.parsed, _ExtractionReport)
    assert result.parsed.obligations != []
    for obligation in result.parsed.obligations:
        assert obligation.severity in {"low", "medium", "high"}
        assert 0.0 <= obligation.confidence <= 1.0
    assert result.model != ""


@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_supports_response_schema_is_true_for_the_configured_live_model() -> None:
    assert (
        _LLM_INTERFACE_MODEL is not None
    )  # narrows type for mypy/ruff; skipif already guards this
    assert supports_response_schema(_LLM_INTERFACE_MODEL) is True
