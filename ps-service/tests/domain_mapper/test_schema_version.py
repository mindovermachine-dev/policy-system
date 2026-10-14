"""Tests for ps_service.domain_mapper's DOMAIN_SCHEMA_VERSION constant."""

from __future__ import annotations

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION


def test_domain_schema_version_is_two() -> None:
    """The schema version is "2": this package's public actions write
    strictly less graph content than a version-"1"-tagged curated
    artifact's replay would produce (governance-artifact derivation is gone).
    """
    assert DOMAIN_SCHEMA_VERSION == "2"
