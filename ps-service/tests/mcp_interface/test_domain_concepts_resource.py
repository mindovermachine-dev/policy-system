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
from importlib import resources
from pathlib import Path

import pytest
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

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


def test_helper_raises_domain_error_on_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    with pytest.raises(McpResourceUnavailableError) as excinfo:
        mcp_server.read_domain_concepts()

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
