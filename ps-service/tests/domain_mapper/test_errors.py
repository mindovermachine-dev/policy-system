"""Tests for ps_service.domain_mapper.errors."""

from __future__ import annotations

import pytest

from ps_service.domain_mapper.errors import (
    DomainMapperConfigurationError,
    DomainMapperDerivationError,
    DomainMapperExtractionError,
    DomainMapperPersistenceError,
    ErrorKind,
)


def test_domain_mapper_extraction_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperExtractionError, Exception)


def test_domain_mapper_derivation_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperDerivationError, Exception)


def test_domain_mapper_persistence_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperPersistenceError, Exception)


def test_domain_mapper_configuration_error_is_exception_subclass() -> None:
    assert issubclass(DomainMapperConfigurationError, Exception)


@pytest.mark.parametrize(
    "error_kind",
    [
        "invalid_definitions_json",
        "missing_terms_key",
        "non_list_terms",
        "invalid_defined_term_item",
    ],
)
def test_domain_mapper_extraction_error_accepts_all_definitions_error_kinds(
    error_kind: ErrorKind,
) -> None:
    exc = DomainMapperExtractionError("x", error_kind=error_kind)
    assert exc.error_kind == error_kind
