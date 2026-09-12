"""Tests for ps_service.llm_interface.client.default_structured_completion_caller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pydantic

from ps_service.llm_interface.client import default_structured_completion_caller

if TYPE_CHECKING:
    import pytest


class _Thing(pydantic.BaseModel):
    value: int


def test_default_structured_completion_caller_forwards_args_to_litellm_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_completion(
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> object:
        captured["model"] = model
        captured["messages"] = messages
        captured["timeout"] = timeout
        captured["response_format"] = response_format
        return sentinel

    monkeypatch.setattr("ps_service.llm_interface.client.litellm.completion", fake_completion)

    messages = [{"role": "user", "content": "hi"}]
    result = default_structured_completion_caller(
        model="fake-model", messages=messages, timeout=30.0, response_format=_Thing
    )

    assert captured == {
        "model": "fake-model",
        "messages": messages,
        "timeout": 30.0,
        "response_format": _Thing,
    }
    assert captured["response_format"] is _Thing
    assert result is sentinel
