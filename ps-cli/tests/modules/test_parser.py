"""Tests for ps_cli.modules.parser: `_celex_type` (PLAN.md §3 Increment 1) and, for
issue #56 Slice 14, `_context_name_type`/`_service_url_type` plus `config set-context`
parsing (PLAN.md §1 D5/D6/D7).

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
    _context_name_type,  # pyright: ignore[reportPrivateUsage]  # issue #56 Slice 14: unit-tested directly per its own AC, mirrors _celex_type
    _instrument_id_type,  # pyright: ignore[reportPrivateUsage]  # issue #66 Slice 7.4: unit-tested directly per its own AC, mirrors _celex_type
    _service_url_type,  # pyright: ignore[reportPrivateUsage]  # issue #56 Slice 14: unit-tested directly per its own AC, mirrors _celex_type
    build_parser,
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


def test_context_name_type_accepts_alnum_start_with_hyphen_and_underscore_body() -> None:
    """A context name starting alnum with `-`/`_` in the body is accepted unchanged (D6)."""
    assert _context_name_type("prod-eu_1") == "prod-eu_1"


def test_context_name_type_rejects_leading_hyphen() -> None:
    """A context name starting with `-` is rejected (D6's alnum-start requirement)."""
    with pytest.raises(argparse.ArgumentTypeError):
        _context_name_type("-prod")


def test_service_url_type_accepts_valid_http_and_https_urls() -> None:
    """A well-formed http(s) URL with a hostname is accepted unchanged (D5)."""
    assert _service_url_type("https://ps.example.com") == "https://ps.example.com"


def test_service_url_type_rejects_url_with_no_scheme() -> None:
    """A URL missing a scheme is rejected via `is_valid_service_url()` (D5)."""
    with pytest.raises(argparse.ArgumentTypeError):
        _service_url_type("ps.example.com")


def test_build_parser_parses_config_set_context_with_url() -> None:
    """`ps-cli config set-context prod --url https://ps.example.com` parses correctly (D7)."""
    args = build_parser().parse_args(
        ["config", "set-context", "prod", "--url", "https://ps.example.com"]
    )

    assert args.command == "config_set_context"
    assert args.name == "prod"
    assert args.url == "https://ps.example.com"


def test_build_parser_config_set_context_with_malformed_url_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed `--url` is rejected by argparse's `type=` callback, exit code 2."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["config", "set-context", "prod", "--url", "not-a-url"])

    assert excinfo.value.code == 2
    assert "not a valid http(s) URL" in capsys.readouterr().err


def test_build_parser_parses_config_use_context() -> None:
    """`ps-cli config use-context prod` parses correctly (D7, issue #56 Slice 25)."""
    args = build_parser().parse_args(["config", "use-context", "prod"])

    assert args.command == "config_use_context"
    assert args.name == "prod"


def test_build_parser_config_use_context_with_leading_hyphen_name_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `use-context` name starting with `-` exits 2, same as `set-context`'s (Slice 25)."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["config", "use-context", "-prod"])

    assert excinfo.value.code == 2


def test_build_parser_parses_config_list_contexts() -> None:
    """`ps-cli config list-contexts` (no arguments) parses correctly (D7, Slice 28)."""
    args = build_parser().parse_args(["config", "list-contexts"])

    assert args.command == "config_list_contexts"


def test_build_parser_config_set_context_with_leading_hyphen_name_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `name` starting with `-` also exits 2 -- argparse treats it as an option-like
    token before `_context_name_type` ever runs, so `name` is reported missing rather
    than format-invalid; still exit code 2 either way, per PLAN.md Slice 14.
    """
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            ["config", "set-context", "-prod", "--url", "https://ps.example.com"]
        )

    assert excinfo.value.code == 2
    assert "name" in capsys.readouterr().err


def test_instrument_id_type_accepts_alnum_hyphen_dot() -> None:
    """A well-formed instrument id like 'CRA-1.0' is accepted unchanged (D13/D17)."""
    assert _instrument_id_type("CRA-1.0") == "CRA-1.0"


def test_instrument_id_type_rejects_leading_hyphen() -> None:
    """An instrument id starting with '-' is rejected (alnum-start requirement)."""
    with pytest.raises(argparse.ArgumentTypeError):
        _instrument_id_type("-CRA-1.0")


def test_instrument_id_type_rejects_path_traversal_segment() -> None:
    """An instrument id containing '..' is rejected -- it is later used to build a
    local filesystem path (`catalog_repo.read_artifact`), so this is the first of two
    defense-in-depth validation layers against a path-traversal payload.
    """
    with pytest.raises(argparse.ArgumentTypeError):
        _instrument_id_type("../../etc")


def test_instrument_id_type_rejects_forward_slash() -> None:
    """An instrument id containing '/' is rejected -- it must be a single path segment."""
    with pytest.raises(argparse.ArgumentTypeError):
        _instrument_id_type("CRA/1.0")


def test_build_parser_parses_catalog_list() -> None:
    """`ps-cli catalog list` (no arguments) parses correctly (D13)."""
    args = build_parser().parse_args(["catalog", "list"])

    assert args.command == "catalog_list"


def test_build_parser_parses_catalog_restore_with_instrument_id() -> None:
    """`ps-cli catalog restore CRA-1.0` parses the positional instrument_id (D13/D17)."""
    args = build_parser().parse_args(["catalog", "restore", "CRA-1.0"])

    assert args.command == "catalog_restore"
    assert args.instrument_id == "CRA-1.0"


def test_build_parser_catalog_restore_with_malformed_instrument_id_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed instrument_id is rejected by argparse's `type=` callback, exit code 2."""
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["catalog", "restore", "../etc"])

    assert excinfo.value.code == 2
    assert "not a valid instrument id" in capsys.readouterr().err
