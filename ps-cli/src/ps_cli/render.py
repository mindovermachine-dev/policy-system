"""Shared bordered-table rendering for ps-cli's read commands.

`handle_get_catalog` (`ps_cli/modules/handlers.py`) and
`handle_config_get_contexts` (`ps_cli/modules/config_handlers.py`) need
*identical* bordered/aligned-table rendering over different data shapes (4
columns vs. 3 columns) -- this module is generic over `headers`/`rows` and
has no knowledge of "catalog" or "context" shapes, per L1 DRY (extract
shared logic into a single location), mirroring the existing
`toml_writer.py` shared-module precedent.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text


def build_table(headers: list[str], rows: list[list[str]]) -> Table:
    """Build a bordered, bold-headered `rich.table.Table` from sanitized headers/rows.

    Exported separately from `print_table()` so a test can render the same `Table`
    through its own forced-color `Console` to prove the header row really carries
    bold styling (AC-BI-003/AC-BI-005) -- `print_table()`'s own auto-detecting
    `Console` never emits color to a non-terminal stream (AC-BI-010), so that assertion
    is otherwise unobservable from `print_table()`'s output alone. Callers with
    zero rows must use `print_table()` instead (AC-BI-007/AC-BI-008); this function does
    not special-case empty `rows`.
    """
    table = Table(show_header=True, header_style="bold")
    for header in headers:
        table.add_column(header)
    for row in rows:
        table.add_row(*(Text(value) for value in row))
    return table


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    r"""Print `headers`/`rows` as a bordered, bold-headered, aligned table to stdout.

    Prints nothing at all if `rows` is empty (AC-BI-007/AC-BI-008, L2 ps-cli Output
    ':192' Silence on success). Every cell is sanitized (control characters
    stripped, AC-BI-009) and wrapped in `rich.text.Text` rather than passed as a raw
    `str` (prevents rich's own `[tag]`-markup interpretation, AC-BI-009) before being
    handed to `build_table()`. Constructs a fresh `Console(file=sys.stdout, width=...)`
    on every call with no `force_terminal` override, so `Console`'s own
    `sys.stdout.isatty()` auto-detection decides whether the header's bold styling is
    emitted as real ANSI SGR codes (interactive terminal) or omitted entirely
    (piped/redirected, AC-BI-010) -- verified empirically that this alone, with no
    extra configuration, produces zero `\x1b[` bytes when `file` is not a tty.
    """
    if not rows:
        return
    sanitized_headers = [_sanitize_cell(header) for header in headers]
    sanitized_rows = [[_sanitize_cell(value) for value in row] for row in rows]
    table = build_table(sanitized_headers, sanitized_rows)
    columns = [
        [sanitized_headers[i]] + [row[i] for row in sanitized_rows]
        for i in range(len(sanitized_headers))
    ]
    width = _console_width_for(*columns)
    Console(file=sys.stdout, width=width).print(table)


def _sanitize_cell(value: str) -> str:
    r"""Strip non-printable characters (AC-BI-009's control-sequence half).

    Wrapping a cell in `Text` (see `build_table()`) stops rich's *own* `[tag]` markup
    syntax from being interpreted, but does nothing about a literal ANSI/control byte
    sequence embedded in the source string itself (e.g. a title containing
    `\x1b[31m`) -- verified empirically that rich passes such bytes through unchanged,
    since they are not part of its markup grammar. `str.isprintable()` returns `False`
    for control characters (ESC, BEL, etc.) while leaving ordinary punctuation/bracket
    characters -- including a literal `[bold]` -- untouched, so this only removes bytes
    that could trigger unintended side effects in a real terminal, never legitimate
    content.
    """
    return "".join(ch for ch in value if ch.isprintable())


def _console_width_for(*columns: list[str]) -> int:
    """Compute a `Console` width wide enough that `Table` never truncates any cell.

    Grounds AC-BI-004, AC-BI-006. `Console`'s default width auto-detects the real
    terminal size, falling back to 80 columns when stdout is not a terminal --
    verified empirically that `Table` silently truncates any cell wider than that
    budget with a trailing "...", which would drop characters from a long instrument
    title or context URL, violating AC-BI-004 ("each row still contains the same four
    fields as before") even though it never raises. Computed per column as the longest
    string in that column
    (header or data) plus 3 (two single-space cell paddings + one border character),
    summed across columns, plus one closing border character -- deterministic and driven
    only by the data actually being printed, never a hardcoded guess (AC-BI-006's literal
    wording).

    Note: width is computed via len() (codepoint count), which undercounts double-width
    Unicode (e.g. CJK) and could still be truncated by Table in that case -- no current
    ps-cli data exercises this, so it is an accepted known limitation, not fixed here.
    """
    return sum(max((len(v) for v in col), default=0) + 3 for col in columns) + 1
