"""The drift detector adds no REST route, no MCP tool and no `/ready` field (issue #215, S5c).

AC-BI-007: drift detection is internal. The structure of the OpenAPI document (every path,
parameter, request and response schema) and of the MCP tool list (names and input schemas) is
compared with a snapshot taken from the commit before the detector existed
(`tests/fixtures/api-surface/service-surface-before-drift-detector.json`, generated from a
`git archive` of that commit). Descriptive text (`description`, `summary`, `title`) is left out of
the comparison so documentation edits do not trip it. A deliberate API change updates the snapshot
in the same commit, with the reason.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

from ps_service.config import ServiceConfig
from ps_service.main import create_app
from ps_service.mcp_interface.mcp_server import server

_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "api-surface"
    / "service-surface-before-drift-detector.json"
)
_PROSE_KEYS = frozenset({"description", "summary", "title"})


def _structure(value: object) -> object:
    """`value` without the prose keys, at every depth."""
    if isinstance(value, dict):
        entries = cast("dict[str, object]", value)
        return {key: _structure(item) for key, item in entries.items() if key not in _PROSE_KEYS}
    if isinstance(value, list):
        return [_structure(item) for item in cast("list[object]", value)]
    return value


def _current_surface() -> dict[str, object]:
    app = create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )
    tools = asyncio.run(server.list_tools())
    return {
        "openapi": _structure(app.openapi()),
        "mcp_tools": sorted(
            ({"name": t.name, "inputSchema": _structure(t.input_schema)} for t in tools),
            key=lambda tool: str(tool["name"]),
        ),
    }


def test_the_openapi_document_and_the_mcp_tool_list_are_those_before_the_detector() -> None:
    expected = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))

    assert _current_surface() == expected


def test_nothing_in_the_public_surface_is_named_after_drift() -> None:
    surface = json.dumps(_current_surface()).lower()

    assert "drift" not in surface
