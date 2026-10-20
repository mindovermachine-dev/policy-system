"""MCP-action and background-ingestion failure logs are scrubbed.

``_run_mcp_action``'s residual net and ``_execute_background_ingestion``'s last-resort net each
write one ``failed`` entry through the real ``configure()``d Logging facade, mirrored to stderr.
Their ``detail`` reuses ``stage_failure_detail`` (no ``repr``, no secrets, at most 300
characters) and carries ``exception_type``; the client result stays the generic message.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, cast

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ingestion_runs._fakes import InMemoryIngestionRunStore

from ps_service.api.stage_failure_log import STAGE_FAILURE_DETAIL_MAX_LEN
from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_UNEXPECTED = "error: an unexpected error occurred"
_ACTION = "some_mcp_action"
_BG = "background_ingestion_run"
_NOISY = (
    "Authorization: Bearer abc.def.ghi api_key=sk-123456 "
    "GET https://secret.internal/graph?x=1 host redis:6379 at /var/lib/ps/data.db"
)
_LEAKS = ("abc.def.ghi", "sk-123456", "secret.internal", "6379", "data.db")


class _BoomError(Exception):
    """A project-unknown exception type."""


@pytest.fixture
def mirrored(tmp_path: Path) -> Iterator[Path]:
    """A real default emitter with failure mirroring on, restored afterwards."""
    log_path = tmp_path / "failures.jsonl"
    saved = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    facade.configure(log_path=log_path, mirror_failures_to_stderr=True)
    try:
        yield log_path
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved  # pyright: ignore[reportPrivateUsage]


def _failed_entries(text: str, action: str) -> list[dict[str, object]]:
    """The ``mcp_interface`` ``failed`` JSON lines for ``action`` in ``text``."""
    parsed = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [
        e
        for e in parsed
        if e.get("component") == "mcp_interface"
        and e.get("action") == action
        and e.get("outcome") == "failed"
    ]


def _chained(message: str) -> Exception:
    """An exception whose message wraps the repr of its cause (a ``Cause(...)`` wrapper)."""
    inner = ValueError(message)
    outer = _BoomError(f"wrapper failed: {inner!r}")
    outer.__cause__ = inner
    return outer


def _raiser(exc: Exception) -> Callable[[], str]:
    def _body() -> str:
        raise exc

    return _body


def _no_repr(entry: dict[str, object], exc: Exception) -> None:
    for value in entry.values():
        assert repr(exc) not in str(value)
        assert "_BoomError(" not in str(value)


def test_run_mcp_action_logs_scrubbed_detail_type_and_run_id(
    mirrored: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One failed entry, scrubbed and capped, mirrored; client result generic."""
    exc = _chained(_NOISY)
    run_id = str(uuid.uuid4())

    result = mcp_server._run_mcp_action(  # pyright: ignore[reportPrivateUsage]
        _ACTION, _raiser(exc), run_id=run_id
    )
    facade.reset_for_tests()

    assert result == _UNEXPECTED
    for sink in (mirrored.read_text(encoding="utf-8"), capsys.readouterr().err):
        [entry] = _failed_entries(sink, _ACTION)
        assert entry["run_id"] == run_id
        assert entry["exception_type"] == "_BoomError"
        detail = str(entry["detail"])
        assert detail.strip()
        assert 0 < len(detail) <= STAGE_FAILURE_DETAIL_MAX_LEN
        for leaked in _LEAKS:
            assert leaked not in sink
        _no_repr(entry, exc)


def test_run_mcp_action_detail_is_capped(mirrored: Path) -> None:
    """A 5000-character message is cut to at most 300 characters."""
    mcp_server._run_mcp_action(  # pyright: ignore[reportPrivateUsage]
        _ACTION, _raiser(_BoomError("x" * 5000))
    )
    facade.reset_for_tests()

    [entry] = _failed_entries(mirrored.read_text(encoding="utf-8"), _ACTION)
    assert 0 < len(str(entry["detail"])) <= STAGE_FAILURE_DETAIL_MAX_LEN


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=REVIEW_THRESHOLD,
        company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
    )


def _submitted_run(store: InMemoryIngestionRunStore) -> str:
    run_id = str(uuid.uuid4())
    store.create_run(run_id=run_id, celex="32022R2065", short_name="dsa", actor=("sub", "iss"))
    return run_id


def _run_background(store: InMemoryIngestionRunStore, run_id: str) -> None:
    mcp_server._execute_background_ingestion(  # pyright: ignore[reportPrivateUsage]
        cast("mcp_server._PreparedCatalogIngestion", object()),  # pyright: ignore[reportPrivateUsage]
        run_id=run_id,
        config=_config(),
        store=store,
    )


def test_background_body_failure_is_scrubbed_and_run_recorded_failed(
    mirrored: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline exception logs under the submitted run_id; the row is failed."""
    exc = _chained(_NOISY)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise exc

    # A pipeline exception of arbitrary type has no real trigger in-process; fault-inject it.
    # detroit-exception: legitimate fault-injection carve-out, see comment above.
    monkeypatch.setattr(mcp_server, "_run_prepared_catalog_ingestion", _boom)
    store = InMemoryIngestionRunStore()
    run_id = _submitted_run(store)

    _run_background(store, run_id)
    facade.reset_for_tests()

    text = mirrored.read_text(encoding="utf-8")
    [entry] = _failed_entries(text, _BG)
    assert entry["run_id"] == run_id
    assert entry["exception_type"] == "_BoomError"
    for leaked in _LEAKS:
        assert leaked not in text
    _no_repr(entry, exc)
    row = store.rows[run_id]
    assert row.status == "failed"
    assert row.error == _UNEXPECTED


def test_background_last_resort_net_entry_is_scrubbed_carries_no_principal_and_run_recorded_failed(
    mirrored: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception escaping ``_run_mcp_action`` is scrubbed and logged with no principal."""
    exc = _chained(_NOISY)

    def _escapes(*_args: object, **_kwargs: object) -> object:
        raise exc

    # `_run_mcp_action` swallows every body exception; the net only fires if the wrapper itself
    # fails, which has no real trigger, so the wrapper is faulted directly.
    # detroit-exception: legitimate fault-injection carve-out, see comment above.
    monkeypatch.setattr(mcp_server, "_run_mcp_action", _escapes)
    store = InMemoryIngestionRunStore()
    run_id = _submitted_run(store)

    _run_background(store, run_id)
    facade.reset_for_tests()

    for sink in (mirrored.read_text(encoding="utf-8"), capsys.readouterr().err):
        [entry] = _failed_entries(sink, _BG)
        assert entry["run_id"] == run_id
        assert entry["exception_type"] == "_BoomError"
        assert "principal" not in entry
        assert 0 < len(str(entry["detail"])) <= STAGE_FAILURE_DETAIL_MAX_LEN
        for leaked in _LEAKS:
            assert leaked not in sink
        _no_repr(entry, exc)
    row = store.rows[run_id]
    assert row.status == "failed"
    assert row.error == _UNEXPECTED
