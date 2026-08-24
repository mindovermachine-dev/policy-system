"""Tests for ps_service.llm_interface.models."""

from __future__ import annotations

import pydantic
import pytest
from ps_service.llm_interface.models import ChatMessage


def test_chat_message_rejects_empty_content() -> None:
    with pytest.raises(pydantic.ValidationError):
        ChatMessage(role="user", content="")
