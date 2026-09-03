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


def escape_basic_string(value: str) -> str:
    r"""Escape `value` for use inside a TOML basic (double-quoted) string body.

    Backslash and double-quote are escaped per the TOML spec; `\t`/`\n`/`\r`
    control characters are escaped defensively even though URLs/context names/
    credentials are not expected to contain them. The caller wraps the returned
    text in double quotes (this function does not add them) — see PLAN.md D16.
    """
    return "".join(_BASIC_STRING_ESCAPES.get(char, char) for char in value)


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
