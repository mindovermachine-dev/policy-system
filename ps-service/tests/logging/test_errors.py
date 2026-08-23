"""Tests for ps_service.logging.errors — M15 sanity."""

from __future__ import annotations

from ps_service.logging.errors import LoggingConfigurationError, LoggingLifecycleError


def test_logging_errors_when_raised_then_are_distinct_exception_types() -> None:
    assert issubclass(LoggingConfigurationError, Exception)
    assert issubclass(LoggingLifecycleError, Exception)
    assert LoggingConfigurationError is not LoggingLifecycleError
    assert not issubclass(LoggingConfigurationError, LoggingLifecycleError)
    assert not issubclass(LoggingLifecycleError, LoggingConfigurationError)
