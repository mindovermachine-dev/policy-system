"""Tests for ps_service.ingestion.adapters.errors."""

from __future__ import annotations

from ps_service.ingestion.adapters.errors import (
    CellarFetchError,
    CellarNotFoundError,
    CellarParseError,
)


def test_cellar_fetch_error_is_exception_subclass() -> None:
    assert issubclass(CellarFetchError, Exception)


def test_cellar_parse_error_is_exception_subclass() -> None:
    assert issubclass(CellarParseError, Exception)


def test_cellar_not_found_error_is_a_cellar_fetch_error() -> None:
    assert issubclass(CellarNotFoundError, CellarFetchError)
