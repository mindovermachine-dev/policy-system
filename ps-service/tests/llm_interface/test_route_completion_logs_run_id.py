"""AC-004 test: a RouteCompletion call emits a structured log entry that includes the bound run_id."""

from __future__ import annotations

from litellm.types.utils import Choices, Message, ModelResponse
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import bind_run_context


def test_route_completion_when_run_id_bound_then_emits_log_entry_with_bound_run_id(
    make_emitter, read_lines
) -> None:
    emitter, log_path = make_emitter()
    fake_response = ModelResponse(
        id="x",
        model="fake-model",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="hi", role="assistant"))],
    )

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    with bind_run_context("run-llm-004"):
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written — wiring bug"
    assert lines[-1]["run_id"] == "run-llm-004"
    assert lines[-1]["action"] == "route_completion"
