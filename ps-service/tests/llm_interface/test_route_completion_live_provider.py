"""AC-001 live test: RouteCompletion against the real, configured LLM Provider."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage, CompletionResult
from ps_service.logging.emitter import EmitterConfig, LogEmitter

pytestmark = pytest.mark.llm_live

# Captured at module-import time (collection), before the autouse `_isolate_logging`
# fixture in `tests/conftest.py` runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)`
# for every test. Reading `os.environ["PS_LLMINTERFACE_MODEL"]` inside the test body
# itself would KeyError once that fixture has run — the guard exists precisely to
# keep leaked `.env` values out of unrelated tests, and this live test's whole point
# is to use the real value, so it must be captured before the fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """A real `LogEmitter` writing to a per-test tmp path — `route_completion`'s `_log` call

    needs a live emitter (or a configured process default) or it raises
    `LoggingLifecycleError`; this test doesn't assert on log content, only
    that `route_completion` itself behaves correctly, so a throwaway emitter
    is enough.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


@pytest.mark.skipif(
    not _LLM_INTERFACE_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_route_completion_returns_nonempty_text_from_live_provider(emitter: LogEmitter) -> None:
    assert _LLM_INTERFACE_MODEL is not None  # narrows type for mypy/ruff; skipif already guards this
    result = route_completion(
        [ChatMessage(role="user", content="Reply with a short greeting.")],
        model=_LLM_INTERFACE_MODEL,
        call_completion=None,
        emitter=emitter,
    )

    assert isinstance(result, CompletionResult)
    assert result.text != ""
    assert result.text.strip() != ""
