"""JSON serializer reproducing the hand-edited layout of the committed intake schema.

Compact where it reads well (short scalar lists and small objects stay on one line),
expanded otherwise. The rules are derived from the committed file; the only input that
cannot be derived is `EXPANDED_THEN_LABELS`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TypeIs

_INDENT = "  "
_WIDTH = 120
_MAX_UNCONDITIONAL_INLINE_ITEMS = 10
_MIN_KEYS_FOR_NESTING_RULES = 2

# Layout quirk of the committed file: the `then` of these labels' `if/then` entries is
# written expanded although it would fit on one line. Pinned by the golden test.
EXPANDED_THEN_LABELS = frozenset(
    {"RegulatoryInstrument", "Requirement", "Obligation", "Capability"}
)

type Json = Mapping[str, Json] | Sequence[Json] | str | int | float | bool | None


def _scalar(value: Json) -> str:
    return json.dumps(value, ensure_ascii=False)


def _is_list(value: Json) -> TypeIs[Sequence[Json]]:
    return isinstance(value, Sequence) and not isinstance(value, str)


def _is_container(value: Json) -> bool:
    return isinstance(value, Mapping) or _is_list(value)


def _inline(value: Json) -> str:
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        body = ", ".join(f"{_scalar(key)}: {_inline(item)}" for key, item in value.items())
        return f"{{ {body} }}"
    if _is_list(value):
        return "[" + ", ".join(_inline(item) for item in value) + "]"
    return _scalar(value)


def _has_multi_key_dict(value: Json) -> bool:
    """True for a dict with >= 2 keys, or any value that contains one."""
    if _is_list(value):
        return any(_has_multi_key_dict(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    return len(value) >= _MIN_KEYS_FOR_NESTING_RULES or any(
        _has_multi_key_dict(item) for item in value.values()
    )


def _is_flat_enough(value: Mapping[str, Json]) -> bool:
    """Rules (b) and (c): no nested multi-key dict, and no dict value beside other keys."""
    values = list(value.values())
    if any(_is_list(item) and any(_is_container(i) for i in item) for item in values):
        return False
    if any(_has_multi_key_dict(item) for item in values):
        return False
    return not (
        len(value) >= _MIN_KEYS_FOR_NESTING_RULES and any(isinstance(i, Mapping) for i in values)
    )


def _forced_expanded_key(value: Mapping[str, Json]) -> str | None:
    """The key whose value must be expanded: `then` of a hinted `if/then` pair."""
    condition = value.get("if")
    if "then" not in value or not isinstance(condition, Mapping):
        return None
    properties = condition.get("properties")
    label = properties.get("label") if isinstance(properties, Mapping) else None
    constant = label.get("const") if isinstance(label, Mapping) else None
    return "then" if constant in EXPANDED_THEN_LABELS else None


def _fits(text: str, level: int, prefix: str, trailer: str) -> bool:
    return len(_INDENT * level + prefix + text + trailer) <= _WIDTH


def _should_inline(value: Json, level: int, prefix: str, trailer: str) -> bool:
    if _is_list(value):
        if any(_is_container(item) for item in value):
            return False
        return len(value) <= _MAX_UNCONDITIONAL_INLINE_ITEMS or _fits(
            _inline(value), level, prefix, trailer
        )
    if not isinstance(value, Mapping):
        return True
    return not value or (_is_flat_enough(value) and _fits(_inline(value), level, prefix, trailer))


def _children(value: Mapping[str, Json] | Sequence[Json]) -> list[tuple[str, Json, bool]]:
    if _is_list(value):
        return [("", item, False) for item in value]
    forced = _forced_expanded_key(value)
    return [(f"{_scalar(key)}: ", item, key == forced) for key, item in value.items()]


def _lines(value: Json, level: int, prefix: str, trailer: str, *, force_expand: bool) -> list[str]:
    pad = _INDENT * level
    if not force_expand and _should_inline(value, level, prefix, trailer):
        return [f"{pad}{prefix}{_inline(value)}{trailer}"]
    if not (isinstance(value, Mapping) or _is_list(value)):  # unreachable: scalars always inline
        return [f"{pad}{prefix}{_scalar(value)}{trailer}"]
    opening, closing = ("{", "}") if isinstance(value, Mapping) else ("[", "]")
    children = _children(value)
    lines = [f"{pad}{prefix}{opening}"]
    for index, (child_prefix, child, forced) in enumerate(children):
        child_trailer = "," if index < len(children) - 1 else ""
        lines.extend(_lines(child, level + 1, child_prefix, child_trailer, force_expand=forced))
    lines.append(f"{pad}{closing}{trailer}")
    return lines


def dumps_hand_layout(value: Json) -> str:
    """Render `value` in the committed hand layout, newline-terminated."""
    return "\n".join(_lines(value, 0, "", "", force_expand=True)) + "\n"
