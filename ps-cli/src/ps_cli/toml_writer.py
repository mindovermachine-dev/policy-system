"""ps-cli's minimal, hand-rolled TOML writer: escaper + flat-table formatter.

New module introduced by issue #56. Python's stdlib `tomllib` (`ps-cli`'s sole TOML
dependency) is read-only — there is no stdlib TOML writer. Rather than add a new
runtime dependency (`tomli-w`, `toml`) for a two-line-shaped serialization need, this
module provides the minimal pair of functions both `targets.py::write_targets()` and
`credentials.py`'s file writer need. See PLAN.md (issue #56) §1 D16.
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
    function does not add them) — see PLAN.md D16.
    """
    return "".join(_escape_char(char) for char in value)


def format_flat_table(table_name: str, pairs: dict[str, str]) -> str:
    """Render `[table_name]` followed by one `key = "value"` line per pair, sorted by key.

    Keys are never quoted — callers only pass keys already known to be safe bare TOML
    keys by construction (e.g. D6's context-name charset); only values pass through
    `escape_basic_string`. Sorted by key for deterministic, diff-friendly output. See
    PLAN.md D16.
    """
    lines = [f"[{table_name}]"]
    lines.extend(f'{key} = "{escape_basic_string(pairs[key])}"' for key in sorted(pairs))
    return "\n".join(lines) + "\n"
