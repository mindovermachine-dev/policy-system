"""Tests for `ps_service.change_monitor.errors`."""

from __future__ import annotations

import pytest

from ps_service.change_monitor.errors import (
    CellarConsolidationQueryError,
    ChangeMonitorConfigurationError,
    ChangeMonitorError,
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
    SuccessionPersistenceError,
)

_SUBCLASSES: list[type[ChangeMonitorError]] = [
    ChangeMonitorConfigurationError,
    CellarConsolidationQueryError,
    SuccessionPersistenceError,
    NationalTranspositionNotSupportedError,
    ChangeMonitorStateError,
]


def test_base_error_is_an_exception_subclass() -> None:
    assert issubclass(ChangeMonitorError, Exception)


@pytest.mark.parametrize("error_type", _SUBCLASSES)
def test_every_component_error_subclasses_the_base(
    error_type: type[ChangeMonitorError],
) -> None:
    assert issubclass(error_type, ChangeMonitorError)


def test_the_hierarchy_has_exactly_five_subclasses() -> None:
    assert set(ChangeMonitorError.__subclasses__()) == set(_SUBCLASSES)


def test_national_transposition_error_default_message_names_issues_41_and_46() -> None:
    error = NationalTranspositionNotSupportedError()

    assert "#41" in str(error)
    assert "#46" in str(error)


def test_national_transposition_error_docstring_names_issues_41_and_46() -> None:
    doc = NationalTranspositionNotSupportedError.__doc__ or ""

    assert "#41" in doc
    assert "#46" in doc


def test_national_transposition_error_accepts_a_custom_message() -> None:
    error = NationalTranspositionNotSupportedError("bespoke context")

    assert str(error) == "bespoke context"


def test_national_transposition_error_is_raisable_and_catchable_as_base() -> None:
    with pytest.raises(ChangeMonitorError):
        raise NationalTranspositionNotSupportedError
