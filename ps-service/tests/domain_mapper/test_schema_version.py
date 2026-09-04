"""Tests for ps_service.domain_mapper's DOMAIN_SCHEMA_VERSION constant."""

from __future__ import annotations

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION


def test_domain_schema_version_is_one() -> None:
    assert DOMAIN_SCHEMA_VERSION == "1"
