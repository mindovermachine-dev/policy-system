"""Tests for ps_service.llm_interface.connectivity.check_connectivity."""

from __future__ import annotations

import httpx
import openai
import pytest
from litellm.types.utils import (
    Choices,
    Embedding,
    EmbeddingResponse,
    Message,
    ModelResponse,
)
from ps_service import config as config_module
from ps_service.config import ServiceConfig
from ps_service.dependency_health import LLM_INTERFACE, is_healthy
from ps_service.llm_interface import _logging_support
from ps_service.llm_interface import completion as completion_module
from ps_service.llm_interface import embedding as embedding_module
from ps_service.llm_interface.connectivity import check_connectivity
from ps_service.llm_interface.errors import LlmProviderError


@pytest.fixture(autouse=True)
def _stub_emit_log_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check_connectivity` calls `route_completion`/`route_embedding` with no
    `emitter=` (mirroring how `main.py`'s lifespan calls it, after `Logging.
    configure()` has already run), so `_log`'s `emit_log_entry` call needs
    either a process default emitter or (as here) a stub — avoids these
    tests depending on the real `Logging.configure()`'s process-wide,
    once-ever `atexit.register` side effect, which other test files'
    ordering-sensitive assertions rely on staying untouched."""
    monkeypatch.setattr(_logging_support, "emit_log_entry", lambda **kwargs: None)


def _config(*, model: str | None = "fake/model", embed_model: str | None = "fake/embed-model") -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model=model,
        llm_interface_embed_model=embed_model,
    )


def _stub_successful_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    completion_response = ModelResponse(
        id="x",
        model="fake/model",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="pong", role="assistant"))],
    )
    embedding_response = EmbeddingResponse(
        model="fake/embed-model",
        data=[Embedding(embedding=[0.1], index=0, object="embedding")],
    )
    monkeypatch.setattr(
        completion_module,
        "default_completion_caller",
        lambda *, model, messages, timeout: completion_response,
    )
    monkeypatch.setattr(
        embedding_module,
        "default_embedding_caller",
        lambda *, model, input, timeout: embedding_response,
    )


def test_check_connectivity_raises_without_marking_a_call_when_completion_model_unconfigured() -> None:
    config = _config(model=None)

    with pytest.raises(LlmProviderError, match="PS_LLMINTERFACE_MODEL"):
        check_connectivity(config)

    assert is_healthy(LLM_INTERFACE) is False


def test_check_connectivity_raises_when_embed_model_unconfigured() -> None:
    config = _config(embed_model=None)

    with pytest.raises(LlmProviderError, match="PS_LLMINTERFACE_EMBED_MODEL"):
        check_connectivity(config)

    assert is_healthy(LLM_INTERFACE) is False


def test_check_connectivity_marks_healthy_when_both_models_configured_and_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_provider_calls(monkeypatch)
    config = _config()

    check_connectivity(config)

    assert is_healthy(LLM_INTERFACE) is True


def test_check_connectivity_marks_unhealthy_when_completion_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_completion_caller(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    monkeypatch.setattr(completion_module, "default_completion_caller", failing_completion_caller)
    config = _config()

    with pytest.raises(LlmProviderError):
        check_connectivity(config)

    assert is_healthy(LLM_INTERFACE) is False


def test_check_connectivity_uses_configured_env_supplied_models_not_hardcoded_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4-style proof (mirrors falkordb_client's connect_from_config test):
    `check_connectivity` must call `route_completion`/`route_embedding` with
    `config`'s own model strings, not a literal — parametrized here via
    `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` env vars through
    the real `load_config()`."""
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", "azure/gpt-5.4-mini")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embed-3")
    captured_models: list[str] = []

    def fake_completion_caller(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        captured_models.append(model)
        return ModelResponse(
            id="x",
            model=model,
            choices=[Choices(finish_reason="stop", index=0, message=Message(content="pong", role="assistant"))],
        )

    def fake_embedding_caller(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        captured_models.append(model)
        return EmbeddingResponse(model=model, data=[Embedding(embedding=[0.1], index=0, object="embedding")])

    monkeypatch.setattr(completion_module, "default_completion_caller", fake_completion_caller)
    monkeypatch.setattr(embedding_module, "default_embedding_caller", fake_embedding_caller)

    config = config_module.load_config()
    check_connectivity(config)

    assert captured_models == ["azure/gpt-5.4-mini", "azure/text-embed-3"]
