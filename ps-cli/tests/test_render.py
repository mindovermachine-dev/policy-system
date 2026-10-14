"""Tests for ps_cli.render."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from rich.console import Console

from ps_cli import render
from ps_cli.render import (
    _console_width_for,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _sanitize_cell,  # pyright: ignore[reportPrivateUsage] — internal helper under test
)

if TYPE_CHECKING:
    import pytest


def test_print_table_prints_headers_values_and_aligned_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`print_table()`'s own first end-to-end (module-input -> stdout) test."""
    render.print_table(["A", "B"], [["1", "two"], ["longer-value", "3"]])

    out = capsys.readouterr().out
    assert "A" in out
    assert "B" in out
    assert "1" in out
    assert "two" in out
    assert "longer-value" in out
    assert "3" in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len({len(line) for line in lines}) == 1  # every line same length -> aligned


def test_print_table_with_no_rows_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`print_table`'s own empty-`rows` guard prints nothing at all."""
    render.print_table(["A"], [])

    assert capsys.readouterr().out == ""


def test_print_table_renders_markup_and_control_sequences_literally(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rich `[tag]` markup and raw ANSI/control bytes render as inert text."""
    render.print_table(
        ["Title"],
        [["[bold]Injected[/bold]"], ["\x1b[31mFakeAnsi\x1b[0m"]],
    )

    out = capsys.readouterr().out
    assert "[bold]Injected[/bold]" in out  # literal, not bold-and-stripped
    assert "\x1b" not in out  # ESC byte itself removed
    assert "[31mFakeAnsi[0m" in out  # surrounding printable chars survive


def test_sanitize_cell_strips_control_characters() -> None:
    # `_sanitize_cell`'s isprintable() filter only removes the ESC (\x1b) and BEL
    # (\x07) control bytes themselves, not the printable "[31m" sequence that
    # follows ESC, so the expected value keeps that text as literal characters.
    assert _sanitize_cell("A\x1b[31mB\x07C") == "A[31mBC"
    assert _sanitize_cell("[bold]plain[/bold]") == "[bold]plain[/bold]"


def test_console_width_for_scales_with_longest_value() -> None:
    narrow = _console_width_for(["a"], ["bb"])
    wide = _console_width_for(["a" * 50], ["bb"])

    assert wide > narrow
    assert wide == (50 + 3) + (2 + 3) + 1


def test_build_table_header_is_bold_styled_on_a_real_terminal() -> None:
    """The header row carries real bold ANSI styling when rendered
    to an actual terminal -- unobservable via `print_table()`'s own auto-detecting
    Console against capsys's non-tty stream (see the sibling no-leak test below), so this
    test builds the same `Table` `print_table()` would and renders it through a
    test-local, explicitly forced-color Console instead.
    """
    table = render.build_table(["Instrument ID"], [["CRA-1.0"]])
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="standard", width=40).print(table)

    out = buf.getvalue()
    assert "\x1b[1m" in out  # bold SGR present on the header


def test_print_table_leaks_no_ansi_when_stdout_is_not_a_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pytest's capsys substitutes a non-tty stream for sys.stdout, exactly
    the piped/redirected shape this AC describes -- print_table's own Console must
    auto-detect that and never emit ANSI, header styling included.
    """
    render.print_table(["Instrument ID", "Title"], [["CRA-1.0", "Cyber Resilience Act"]])

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "Instrument ID" in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len({len(line) for line in lines}) == 1


def test_print_table_does_not_truncate_a_wide_value_and_keeps_rows_aligned(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 120-char value prints verbatim, no truncation; short row is padded."""
    wide_value = "A" * 120
    render.print_table(["Instrument ID", "Title"], [["CRA-1.0", wide_value], ["X-2.0", "short"]])

    out = capsys.readouterr().out
    assert wide_value in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len({len(line) for line in lines}) == 1  # every line same length -> aligned
