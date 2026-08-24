"""Tests for ps_service.ingestion.errors."""

from __future__ import annotations

from ps_service.ingestion.errors import (
    IngestionConfigurationError,
    IngestionPersistenceError,
)


def test_ingestion_persistence_error_is_exception_subclass() -> None:
    assert issubclass(IngestionPersistenceError, Exception)


def test_ingestion_configuration_error_is_exception_subclass() -> None:
    assert issubclass(IngestionConfigurationError, Exception)
