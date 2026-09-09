"""Tests for ps_service.domain_mapper's DOMAIN_SCHEMA_VERSION constant."""

from __future__ import annotations

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION


def test_domain_schema_version_is_two() -> None:
    """GH #76 bumped this from "1" to "2": `derive_governance_artifacts`
    (issue #54 S3) was deleted outright, so this package's public actions now
    write strictly less graph content than a version-"1"-tagged curated
    artifact's replay would have produced.
    """
    assert DOMAIN_SCHEMA_VERSION == "2"
