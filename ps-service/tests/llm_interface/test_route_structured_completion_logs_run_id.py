"""AC-BI-016 test: a RouteStructuredCompletion call emits a log entry with the bound run_id."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pydantic
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.llm_interface.models import ChatMessage
from ps_service.llm_interface.structured_completion import route_structured_completion
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    import pytest
    from conftest import MakeEmitter, ReadLines


class _Extraction(pydantic.BaseModel):
    """A minimal response model used only by this test."""

    label: str


def test_route_structured_completion_when_run_id_bound_then_emits_log_entry_with_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_supports_response_schema(model: str, custom_llm_provider: str | None = None) -> bool:
        return True

    monkeypatch.setattr(
        "ps_service.llm_interface.structured_completion.supports_response_schema",
        _fake_supports_response_schema,
    )

    emitter, log_path = make_emitter()
    fake_response = ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop",
                index=0,
                message=Message(content='{"label": "ok"}', role="assistant"),
            )
        ],
    )

    def fake_call_completion(
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> ModelResponse:
        return fake_response

    with bind_run_context("run-llm-024"):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=fake_call_completion,
            emitter=emitter,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written — wiring bug"
    assert lines[-1]["run_id"] == "run-llm-024"
    assert lines[-1]["action"] == "route_structured_completion"
