"""ps-cli's minimal, hand-rolled TOML writer: escaper + array/flat-table formatters.

Python's stdlib `tomllib` (`ps-cli`'s sole TOML dependency) is read-only — there is no
stdlib TOML writer. Rather than add a runtime dependency (`tomli-w`, `toml`) for a
small serialization need, this module provides `escape_basic_string` (the one escaper
every writer needs), `format_flat_table` (a flat single-level table formatter), and
`format_string_array` (for `auth.scopes`). Nested per-context tables
(`targets.py::write_targets()`'s `[contexts.<name>]`/`[contexts.<name>.auth]`,
`credentials.py`'s `[credentials.<context>]`) are hand-rolled inline at each of their
two call sites rather than generalized into a shared nested-table writer, because the
codebase extracts shared code only once a pattern repeats a third time.
"""

from __future__ import annotations

_BASIC_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
}

_C0_CONTROL_LIMIT = 0x20  # exclusive upper bound of the C0 control block (U+0000-U+001F)
_DEL = 0x7F


def _escape_char(char: str) -> str:
    """Escape a single character per the TOML basic-string rule, or pass it through."""
    if char in _BASIC_STRING_ESCAPES:
        return _BASIC_STRING_ESCAPES[char]
    codepoint = ord(char)
    # TOML basic strings must escape every control character (U+0000-U+001F) and
    # U+007F (DEL); literal tab (U+0009) is the one control char TOML permits
    # unescaped, but it's already covered by the explicit short-form map above.
    if codepoint < _C0_CONTROL_LIMIT or codepoint == _DEL:
        return f"\\u{codepoint:04x}"
    return char


def escape_basic_string(value: str) -> str:
    r"""Escape `value` for use inside a TOML basic (double-quoted) string body.

    Backslash and double-quote are escaped per the TOML spec. Every control
    character (U+0000-U+001F, plus U+007F DEL) must also be escaped per the TOML
    spec — `\t`/`\n`/`\r` use their short-form escapes for readability (TOML permits
    this even though `\uXXXX` would also work); every other control character
    (ESC, BEL, NUL, DEL, ...) uses a `\uXXXX` unicode escape, since TOML has no
    short form for them. The caller wraps the returned text in double quotes (this
    function does not add them).

    Parameters:
        value: Any string; may be empty or contain non-ASCII text (passed through).

    Returns:
        The escaped text, safe between double quotes in a TOML basic string. Never
        raises and has no side effects.
    """
    return "".join(_escape_char(char) for char in value)


def format_flat_table(table_name: str, pairs: dict[str, str]) -> str:
    """Render `[table_name]` followed by one `key = "value"` line per pair, sorted by key.

    Keys are never quoted — callers only pass keys already known to be safe bare TOML
    keys by construction (e.g. a validated context name); only values pass through
    `escape_basic_string`. Sorted by key for deterministic, diff-friendly output.

    Parameters:
        table_name: Name placed inside the `[...]` header, written verbatim (not
            escaped or validated).
        pairs: Key/value pairs for the table; may be empty.

    Returns:
        The table text ending in a newline. Never raises and has no side effects.
    """
    lines = [f"[{table_name}]"]
    lines.extend(f'{key} = "{escape_basic_string(pairs[key])}"' for key in sorted(pairs))
    return "\n".join(lines) + "\n"


def format_string_array(values: tuple[str, ...]) -> str:
    """Render `values` as a TOML string array: `["a", "b"]`.

    Each element passes through `escape_basic_string` and is wrapped in double quotes;
    the caller places the returned text on the right-hand side of a `key = ...` line
    (this function renders only the array literal itself).

    Parameters:
        values: Strings to render; may be empty (yields `[]`).

    Returns:
        The array literal, e.g. `["a", "b"]`. Never raises and has no side effects.
    """
    return "[" + ", ".join(f'"{escape_basic_string(value)}"' for value in values) + "]"
