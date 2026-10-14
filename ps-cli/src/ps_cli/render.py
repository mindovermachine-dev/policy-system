"""Shared bordered-table rendering for ps-cli's read commands.

`handle_config_get_contexts` (`ps_cli/modules/config_handlers.py`) is this
module's current consumer -- generic over `headers`/`rows`, with no
knowledge of "context" shapes specifically, so that table rendering lives in one
location (L1 DRY), like the shared `toml_writer.py` module.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text


def build_table(headers: list[str], rows: list[list[str]]) -> Table:
    """Build a bordered, bold-headered `rich.table.Table` from sanitized headers and rows.

    Exposed separately from `print_table()` so a test can render the same `Table`
    through its own forced-colour `Console` and prove the header row carries bold
    styling: `print_table()`'s auto-detecting `Console` never emits colour to a
    non-terminal stream, so that is otherwise unobservable. Callers with zero rows
    must use `print_table()`, which prints nothing; this function does not
    special-case empty `rows`.

    Args:
        headers: Column header texts, already sanitized.
        rows: Table rows, each holding one already-sanitized cell text per header.
            Cells are wrapped in `rich.text.Text` so rich does not interpret
            `[tag]` markup in them.

    Returns:
        The populated table; nothing is printed.
    """
    table = Table(show_header=True, header_style="bold")
    for header in headers:
        table.add_column(header)
    for row in rows:
        table.add_row(*(Text(value) for value in row))
    return table


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    r"""Print `headers`/`rows` as a bordered, bold-headered, aligned table to stdout.

    Prints nothing at all when `rows` is empty (silence on success, per the L2
    ps-cli output rules). Every cell is sanitized (control characters stripped) and
    wrapped in `rich.text.Text` so rich's own `[tag]` markup is not interpreted.
    A fresh `Console` is built on every call with no `force_terminal` override, so
    `Console`'s `isatty()` detection decides whether the bold header is emitted as
    ANSI codes (interactive terminal) or omitted (piped or redirected output). The
    console width is computed from the data so no cell is truncated.

    Args:
        headers: Column header texts.
        rows: Table rows, each holding one cell text per header.

    Returns:
        None.

    Side Effects:
        Writes the rendered table to `sys.stdout`; writes nothing when `rows` is
        empty.
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
    r"""Strip non-printable characters from a cell so it cannot inject control sequences.

    Wrapping a cell in `Text` stops rich's own `[tag]` markup from being interpreted
    but does nothing about a literal ANSI or control byte sequence in the source
    string (e.g. a title containing `\x1b[31m`); rich passes such bytes through
    unchanged. `str.isprintable()` is `False` for control characters (ESC, BEL, ...)
    but `True` for ordinary punctuation and brackets, so only bytes that could
    trigger unintended terminal behaviour are removed and legitimate content,
    including a literal `[bold]`, is untouched.

    Args:
        value: The raw cell text.

    Returns:
        `value` without its non-printable characters.
    """
    return "".join(ch for ch in value if ch.isprintable())


def _console_width_for(*columns: list[str]) -> int:
    """Compute a `Console` width wide enough that `Table` never truncates any cell.

    `Console`'s default width auto-detects the terminal size and falls back to 80
    columns when stdout is not a terminal; `Table` then silently truncates any wider
    cell with a trailing "...", dropping characters from a long title or URL. The
    width is therefore computed per column as the longest string in that column
    (header or data) plus 3 (two cell paddings and one border character), summed
    across columns, plus one closing border character. It depends only on the data
    printed.

    Width is measured with `len()` (code points), which undercounts double-width
    Unicode such as CJK, so a table with such text can still be truncated; this is a
    known limitation.

    Args:
        *columns: One list of cell texts per column, header included.

    Returns:
        The console width in character cells; `1` when there are no columns.
    """
    return sum(max((len(v) for v in col), default=0) + 3 for col in columns) + 1
