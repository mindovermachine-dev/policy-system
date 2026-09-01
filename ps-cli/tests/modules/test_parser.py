"""Tests for ps_cli.modules.parser: `_celex_type` (PLAN.md §3 Increment 1).

AC-BI-001/AC-BI-002: leading/trailing whitespace is trimmed before the CELEX
format-validation regex runs, and a value that is still malformed after
trimming raises the same `argparse.ArgumentTypeError` message as it does
today -- trimming must not change the error text.
"""

from __future__ import annotations

import argparse

import pytest

from ps_cli.modules.parser import (
    _celex_type,  # pyright: ignore[reportPrivateUsage]  # PLAN.md Inc. 1: unit-tested directly per its own AC
)


def test_celex_type_trims_leading_and_trailing_whitespace_before_validating() -> None:
    """A well-formed CELEX padded with whitespace is trimmed and accepted."""
    assert _celex_type("  32016R0679  ") == "32016R0679"


def test_celex_type_rejects_malformed_value_after_trim_with_unchanged_error_message() -> None:
    """A value that's still malformed after trimming raises the existing, unchanged message."""
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _celex_type("  not-a-celex  ")

    assert str(excinfo.value) == (
        "'not-a-celex' is not a 10-character CELEX identifier "
        "(expected: 3<4 digits><1 uppercase letter><4 digits>, e.g. 32016R0679)"
    )
