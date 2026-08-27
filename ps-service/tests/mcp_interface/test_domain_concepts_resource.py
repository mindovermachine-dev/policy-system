"""Tests for the `GetDomainConcepts` MCP resource (PLAN_REVIEWED.md §6, Batch 5).

Covers AC-009 (resource listed under a stable URI with a markdown mime type),
AC-010 (read returns the backing file verbatim -- no restructured schema),
AC-011 (a missing / unreadable file surfaces a clean resource-read error, never
a stack trace across the boundary) and AC-012 (client input cannot redirect the
read: zero-parameter helper, fixed absolute path, unknown/traversal URIs are
rejected by the SDK before the read function runs).

`pytest-asyncio` is not installed; server coroutines are driven with bare
`asyncio.run(...)` (PLAN_REVIEWED.md Residual risk 8). Hand-written structural
fakes / monkeypatching only -- no `unittest.mock`.

F-11: `ReadResourceContents.content` is a `str`, not `bytes`.
F-12: on a resource-read failure the mcp SDK emits its own ERROR-level log
record (full traceback + absolute path) to `mcp.server.mcpserver.server` before
re-raising a clean `ResourceError`. The AC-011 test `caplog`-absorbs that record
(sets the level so it neither fails an assertion nor floods output); it is
expected, not asserted against.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import pytest
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.errors import McpResourceUnavailableError

_KNOWN_MARKDOWN = "# PS domain concepts\n\nRegulation → Obligation -- café ✅\n"


@pytest.fixture(autouse=True)
def _clear_domain_concepts_path_cache() -> None:
    """`_domain_concepts_path` is `@functools.cache`d. Tests that monkeypatch the
    module attribute by name replace the whole cached object (no pollution), but
    `test_domain_concepts_path_is_absolute_and_fixed` calls the real one -- clear
    the cache around every test so nothing leaks a cached `Path` between them.
    """
    mcp_server._domain_concepts_path.cache_clear()


def _point_helper_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: path)


def test_read_returns_file_content_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text(_KNOWN_MARKDOWN, encoding="utf-8")
    _point_helper_at(monkeypatch, md_file)

    assert mcp_server._read_domain_concepts() == _KNOWN_MARKDOWN


def test_read_helper_takes_no_parameters() -> None:
    assert inspect.signature(mcp_server._read_domain_concepts).parameters == {}


def test_domain_concepts_path_is_absolute_and_fixed() -> None:
    mcp_server._domain_concepts_path.cache_clear()

    path = mcp_server._domain_concepts_path()

    assert path.is_absolute()
    assert path.as_posix().endswith("docs/artifacts/ps-domain-concepts.md")


def test_helper_raises_domain_error_on_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    with pytest.raises(McpResourceUnavailableError) as excinfo:
        mcp_server._read_domain_concepts()

    message = str(excinfo.value)
    assert "does-not-exist.md" not in message
    assert str(missing) not in message
    assert "Errno" not in message
    assert "Traceback" not in message


def test_resource_listed_with_stable_uri_and_markdown_mime() -> None:
    resources = asyncio.run(mcp_server.server.list_resources())

    matches = [r for r in resources if str(r.uri) == "psdomain://concepts"]
    assert len(matches) == 1
    assert matches[0].mime_type == "text/markdown"


def test_read_resource_via_server_returns_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text(_KNOWN_MARKDOWN, encoding="utf-8")
    _point_helper_at(monkeypatch, md_file)

    raw = asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))
    contents = [c for c in raw if isinstance(c, ReadResourceContents)]

    assert len(contents) == 1
    body = contents[0].content
    assert isinstance(body, str)  # F-11: str, not bytes
    assert body == _KNOWN_MARKDOWN


def test_read_resource_via_server_missing_file_error_is_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)  # F-12: absorb the SDK's own ERROR record
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    # The SDK re-raises a `ResourceError` (no stable public class to pin on);
    # what matters is that its `str()` carries no traceback and no filesystem path.
    with pytest.raises(Exception) as excinfo:
        asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))

    surfaced = str(excinfo.value)
    assert "Traceback" not in surfaced
    assert str(missing) not in surfaced
    assert "does-not-exist.md" not in surfaced


def test_traversal_uri_is_unknown_resource() -> None:
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(mcp_server.server.read_resource("psdomain://../etc/passwd"))
