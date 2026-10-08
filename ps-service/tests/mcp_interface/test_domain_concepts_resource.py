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
AC-BI-017: a missing / unreadable file makes the resource return a path-free
`error:` text body (the private `_load_domain_concepts` raises
`McpResourceUnavailableError`; the registered resource function catches it).
"""

from __future__ import annotations

import asyncio
import inspect
from importlib import resources
from pathlib import Path

import pytest
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import CallToolResult, TextContent

from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.errors import McpResourceUnavailableError

_KNOWN_MARKDOWN = "# PS domain concepts\n\nRegulation → Obligation -- café ✅\n"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clear_domain_concepts_path_cache() -> None:
    """`_domain_concepts_path` is `@functools.cache`d. Tests that monkeypatch the
    module attribute by name replace the whole cached object (no pollution), but
    `test_domain_concepts_path_is_absolute_and_fixed` calls the real one -- clear
    the cache around every test so nothing leaks a cached `Path` between them.
    """
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # test reaches into a module-internal cached helper by design


def _point_helper_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: path)


def test_read_returns_file_content_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text(_KNOWN_MARKDOWN, encoding="utf-8")
    _point_helper_at(monkeypatch, md_file)

    assert mcp_server.read_domain_concepts() == _KNOWN_MARKDOWN


def test_read_helper_takes_no_parameters() -> None:
    assert inspect.signature(mcp_server.read_domain_concepts).parameters == {}


def test_domain_concepts_path_is_absolute_and_fixed() -> None:
    """AC-BI-012: the resource resolves from the installed package, not a repo checkout.

    Asserts the filename and that the resolved location is a descendant of
    `ps_service.mcp_interface`'s own installed package directory -- never
    asserting a `docs/artifacts` substring, which is precisely the
    checkout-relative layout this AC requires removing.
    """
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # test reaches into a module-internal cached helper by design

    path = mcp_server._domain_concepts_path()  # pyright: ignore[reportPrivateUsage]  # test invokes the real module-internal path helper

    assert path.name == "ps-domain-concepts.md"
    package_root = resources.files("ps_service.mcp_interface")
    with (
        resources.as_file(path) as concrete_path,
        resources.as_file(package_root) as concrete_root,
    ):
        assert concrete_path.resolve().is_relative_to(concrete_root.resolve())


def test_packaged_copy_matches_docs_artifacts_source() -> None:
    """AC-BI-012 anti-drift guard: the packaged copy is byte-identical to the source.

    `docs/artifacts/ps-domain-concepts.md` is the checkout-relative canonical
    source -- safe to reference here since tests always run inside a
    checkout, unlike the runtime `_domain_concepts_path()` helper.
    """
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # test reaches into a module-internal cached helper by design

    packaged_content = mcp_server._domain_concepts_path().read_text(  # pyright: ignore[reportPrivateUsage]  # test invokes the real module-internal path helper
        encoding="utf-8"
    )
    source_content = (_REPO_ROOT / "docs" / "artifacts" / "ps-domain-concepts.md").read_text(
        encoding="utf-8"
    )

    assert packaged_content == source_content


def test_load_raises_domain_error_on_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    with pytest.raises(McpResourceUnavailableError) as excinfo:
        mcp_server._load_domain_concepts()  # pyright: ignore[reportPrivateUsage]  # test drives the module-private loader that raises the domain error

    message = str(excinfo.value)
    assert "does-not-exist.md" not in message
    assert str(missing) not in message
    assert "Errno" not in message
    assert "Traceback" not in message


def test_read_returns_clean_error_string_on_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-017 (resource half): a missing file yields a path-free `error:` text."""
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    text = mcp_server.read_domain_concepts()

    assert text.startswith("error:")
    assert "does-not-exist.md" not in text
    assert str(missing) not in text
    assert "Errno" not in text
    assert "Traceback" not in text


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    raw = asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))
    [content] = [c for c in raw if isinstance(c, ReadResourceContents)]

    body = content.content
    assert isinstance(body, str)
    assert body.startswith("error:")
    assert "Traceback" not in body
    assert str(missing) not in body
    assert "does-not-exist.md" not in body


def test_resource_returns_the_real_packaged_document_byte_for_byte() -> None:
    """AC-BI-010: no monkeypatching; the resource is the packaged file, equal to the docs copy."""
    raw = asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))
    [content] = [c for c in raw if isinstance(c, ReadResourceContents)]
    packaged = (
        resources.files("ps_service.mcp_interface")
        .joinpath("ps-domain-concepts.md")
        .read_text(encoding="utf-8")
    )
    source = (_REPO_ROOT / "docs" / "artifacts" / "ps-domain-concepts.md").read_text(
        encoding="utf-8"
    )

    assert content.content == packaged
    assert content.content == source


def _tool_text() -> str:
    outcome = asyncio.run(mcp_server.server.call_tool("domain_concepts", {}))
    assert isinstance(outcome, CallToolResult)
    [text] = [c.text for c in outcome.content if isinstance(c, TextContent)]
    return text


def test_tool_and_resource_differ_and_tool_is_shorter() -> None:
    raw = asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))
    [content] = [c for c in raw if isinstance(c, ReadResourceContents)]
    resource_text = content.content
    assert isinstance(resource_text, str)

    tool_text = _tool_text()

    assert tool_text != resource_text
    assert len(tool_text) < len(resource_text)


def test_tool_still_returns_schema_when_packaged_document_is_missing_and_resource_errors_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    tool_text = _tool_text()
    raw = asyncio.run(mcp_server.server.read_resource("psdomain://concepts"))
    [content] = [c for c in raw if isinstance(c, ReadResourceContents)]

    assert not tool_text.startswith("error:")
    assert "NODES" in tool_text
    assert isinstance(content.content, str)
    assert content.content.startswith("error:")
    assert str(missing) not in content.content


def test_traversal_uri_is_unknown_resource() -> None:
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(mcp_server.server.read_resource("psdomain://../etc/passwd"))
