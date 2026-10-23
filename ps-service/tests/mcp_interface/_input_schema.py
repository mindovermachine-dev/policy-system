"""Helpers that read a registered tool's published input schema with its `$ref`s resolved."""

from __future__ import annotations

import asyncio
from typing import cast

from ps_service.mcp_interface import mcp_server

type JsonObject = dict[str, object]

_DEFS_PREFIX = "#/$defs/"


def _resolve(node: object, defs: JsonObject) -> object:
    """Return `node` with every local `$ref` replaced by the definition it names."""
    if isinstance(node, list):
        return [_resolve(item, defs) for item in cast("list[object]", node)]
    if not isinstance(node, dict):
        return node
    mapping = cast("JsonObject", node)
    ref = mapping.get("$ref")
    if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
        return _resolve(defs[ref.removeprefix(_DEFS_PREFIX)], defs)
    return {key: _resolve(value, defs) for key, value in mapping.items() if key != "$defs"}


def tool_input_schema(tool_name: str) -> JsonObject:
    """Return the input schema `tools/list` publishes for `tool_name`, `$ref`s resolved."""
    tools = asyncio.run(mcp_server.server.list_tools())
    (tool,) = (tool for tool in tools if tool.name == tool_name)
    raw = cast("JsonObject", tool.input_schema)
    defs = cast("JsonObject", raw.get("$defs", {}))
    return cast("JsonObject", _resolve(raw, defs))


def parameter_object_schema(tool_name: str, parameter: str) -> JsonObject:
    """Return the object branch of the nullable `parameter` of `tool_name`."""
    schema = tool_input_schema(tool_name)
    properties = cast("dict[str, JsonObject]", schema["properties"])
    branches = cast("list[JsonObject]", properties[parameter]["anyOf"])
    (branch,) = (item for item in branches if item.get("type") == "object")
    return branch


def parameter_array_item_schema(tool_name: str, parameter: str) -> JsonObject:
    """Return the item schema of the nullable array `parameter` of `tool_name`."""
    schema = tool_input_schema(tool_name)
    properties = cast("dict[str, JsonObject]", schema["properties"])
    branches = cast("list[JsonObject]", properties[parameter]["anyOf"])
    (branch,) = (item for item in branches if item.get("type") == "array")
    return cast("JsonObject", branch["items"])
