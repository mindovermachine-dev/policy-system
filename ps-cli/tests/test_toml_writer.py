"""Tests for ps_cli.toml_writer: escape_basic_string(), format_flat_table().

New module introduced by issue #56, D16: a minimal hand-rolled TOML writer (no new
runtime dependency — stdlib `tomllib` is read-only). Shared by `targets.py::
write_targets()` and `credentials.py`'s file writer. See PLAN.md (issue #56) §1 D16,
§4 Slice 12.
"""

from __future__ import annotations

import tomllib

from ps_cli.toml_writer import escape_basic_string, format_flat_table


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


def test_format_flat_table_renders_header_and_sorted_key_value_lines() -> None:
    """`format_flat_table` renders `[table_name]` then `key = "value"` lines, sorted by key."""
    rendered = format_flat_table("contexts", {"prod": "https://x", "dev": "http://y"})

    assert rendered == '[contexts]\ndev = "http://y"\nprod = "https://x"\n'


def test_format_flat_table_output_round_trips_through_tomllib() -> None:
    """The rendered table text parses back via `tomllib` into the original mapping."""
    pairs = {"dev": "http://127.0.0.1:8000", "prod": "https://ps.example.com"}

    rendered = format_flat_table("contexts", pairs)
    reparsed = tomllib.loads(rendered)

    assert reparsed == {"contexts": pairs}


def test_format_flat_table_with_empty_pairs_renders_header_only() -> None:
    """An empty pairs mapping still renders the `[table_name]` header, with no value lines."""
    rendered = format_flat_table("contexts", {})

    assert rendered == "[contexts]\n"
