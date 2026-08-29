"""Tests for ps_service.llm_interface.client.default_completion_caller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ps_service.llm_interface.client import default_completion_caller

if TYPE_CHECKING:
    import pytest


def test_default_completion_caller_forwards_args_to_litellm_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> object:
        captured["model"] = model
        captured["messages"] = messages
        captured["timeout"] = timeout
        return sentinel

    monkeypatch.setattr("ps_service.llm_interface.client.litellm.completion", fake_completion)

    messages = [{"role": "user", "content": "hi"}]
    result = default_completion_caller(model="fake-model", messages=messages, timeout=30.0)

    assert captured["model"] == "fake-model"
    assert captured["messages"] == messages
    assert captured["timeout"] == 30.0
    assert result is sentinel
