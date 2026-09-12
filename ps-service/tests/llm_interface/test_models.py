"""Tests for ps_service.llm_interface.models."""

from __future__ import annotations

import pydantic
import pytest

from ps_service.llm_interface.models import ChatMessage, StructuredCompletionResult


def test_chat_message_rejects_empty_content() -> None:
    with pytest.raises(pydantic.ValidationError):
        ChatMessage(role="user", content="")


class _Thing(pydantic.BaseModel):
    value: int


def test_structured_completion_result_is_frozen_on_parsed_and_model() -> None:
    result = StructuredCompletionResult(parsed=_Thing(value=1), model="fake-model")

    with pytest.raises(pydantic.ValidationError):
        result.parsed = _Thing(value=2)  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(pydantic.ValidationError):
        result.model = "other-model"  # pyright: ignore[reportAttributeAccessIssue]
