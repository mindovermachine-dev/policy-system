"""Tests for ps_service.llm_interface.client.default_embedding_caller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ps_service.llm_interface.client import default_embedding_caller

if TYPE_CHECKING:
    import pytest


def test_default_embedding_caller_forwards_args_to_litellm_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    # Signature mirrors the real `litellm.embedding`, which itself takes `input`
    # (A002 is ignored tree-wide under tests/ per pyproject per-file-ignores).
    def fake_embedding(*, model: str, input: list[str], timeout: float) -> object:
        captured["model"] = model
        captured["input"] = input
        captured["timeout"] = timeout
        return sentinel

    monkeypatch.setattr("ps_service.llm_interface.client.litellm.embedding", fake_embedding)

    text_input = ["hello world"]
    result = default_embedding_caller(model="fake-embed-model", inputs=text_input, timeout=30.0)

    assert captured["model"] == "fake-embed-model"
    assert captured["input"] == text_input
    assert captured["timeout"] == 30.0
    assert result is sentinel
