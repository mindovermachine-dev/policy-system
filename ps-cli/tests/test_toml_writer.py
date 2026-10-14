"""Tests for ps_cli.toml_writer: escape_basic_string(),
format_string_array().

The module is a minimal hand-rolled TOML writer (no runtime dependency — stdlib
`tomllib` is read-only). `escape_basic_string()` is shared by every writer;
`format_string_array()` serves `targets.py::write_targets()`'s `auth.scopes` field.
"""

from __future__ import annotations

import tomllib

from ps_cli.toml_writer import escape_basic_string, format_string_array


def test_escape_basic_string_round_trips_through_tomllib_when_quoted() -> None:
    """An escaped value, wrapped in double quotes, parses back to the original string."""
    original = 'a "quoted" \\ value'

    escaped = escape_basic_string(original)
    reparsed = tomllib.loads(f'value = "{escaped}"\n')

    assert reparsed == {"value": original}


def test_escape_basic_string_escapes_control_characters() -> None:
    """Tab, newline, and carriage-return characters round-trip through the escaper too."""
    original = "line1\tline2\nline3\rline4"

    escaped = escape_basic_string(original)
    reparsed = tomllib.loads(f'value = "{escaped}"\n')

    assert reparsed == {"value": original}


def test_escape_basic_string_escapes_non_map_control_characters() -> None:
    r"""C0 control bytes outside the 5-entry explicit map (e.g. ESC) are also escaped.

    Regression test: an escape table covering only backslash/quote/tab/newline/CR
    would write any other control byte (ESC `\\x1b`, BEL `\\x07`, NUL, ..., DEL `\\x7f`)
    through unescaped, producing TOML that violates the spec and that `tomllib` then
    rejects on the next read.
    """
    original = "a\x1bb"

    escaped = escape_basic_string(original)

    assert "\x1b" not in escaped
    reparsed = tomllib.loads(f'value = "{escaped}"\n')
    assert reparsed == {"value": original}


def test_escape_basic_string_round_trips_bel_and_del_control_characters() -> None:
    """BEL (U+0007) and DEL (U+007F) — both outside the explicit map — round-trip too."""
    original = "x\x07y\x7fz"

    escaped = escape_basic_string(original)
    reparsed = tomllib.loads(f'value = "{escaped}"\n')

    assert reparsed == {"value": original}


def test_format_string_array_renders_bracketed_quoted_elements() -> None:
    """`format_string_array` renders a TOML string-array literal, elements in order."""
    rendered = format_string_array(("openid", "profile"))

    assert rendered == '["openid", "profile"]'


def test_format_string_array_with_empty_tuple_renders_empty_brackets() -> None:
    """An empty tuple renders `[]`, not `[""]` or a crash."""
    assert format_string_array(()) == "[]"


def test_format_string_array_escapes_each_element_and_round_trips_through_tomllib() -> None:
    """Each element is escaped via `escape_basic_string`; the whole array round-trips."""
    rendered = format_string_array(('a "quoted" value', "plain"))
    reparsed = tomllib.loads(f"value = {rendered}\n")

    assert reparsed == {"value": ['a "quoted" value', "plain"]}
