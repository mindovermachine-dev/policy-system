"""Tests for ps_service.llm_interface.errors."""

from __future__ import annotations

from ps_service.llm_interface.errors import LlmProviderError, LlmResponseSchemaError


def test_llm_provider_error_is_exception_subclass() -> None:
    assert issubclass(LlmProviderError, Exception)


def test_llm_response_schema_error_is_llm_provider_error_subclass() -> None:
    assert issubclass(LlmResponseSchemaError, LlmProviderError)
