"""Tests for ps_service.domain_mapper.errors."""

from __future__ import annotations

from ps_service.domain_mapper.errors import (
    DomainMapperConfigurationError,
    DomainMapperDerivationError,
    DomainMapperExtractionError,
    DomainMapperPersistenceError,
)


def test_domain_mapper_extraction_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperExtractionError, Exception)


def test_domain_mapper_derivation_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperDerivationError, Exception)


def test_domain_mapper_persistence_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperPersistenceError, Exception)


def test_domain_mapper_configuration_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperConfigurationError, Exception)
