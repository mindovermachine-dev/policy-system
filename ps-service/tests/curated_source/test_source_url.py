"""Tests for ``ps_service.curated_source.source_url.validate_source_url`` (D-VALIDATION)."""

from __future__ import annotations

import pytest

from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.source_url import validate_source_url


def test_validate_source_url_accepts_https_url() -> None:
    """AC-BI-010: a plain `https://` URL is always accepted."""
    url = "https://raw.githubusercontent.com/example/repo/main/curated-content"

    assert validate_source_url(url, allow_insecure_http=False) == url


def test_validate_source_url_rejects_plain_http_by_default() -> None:
    """AC-BI-010: TLS is required by default -- plain `http://` needs explicit opt-in."""
    with pytest.raises(CuratedSourceConfigurationError) as excinfo:
        validate_source_url("http://example.com/curated-content", allow_insecure_http=False)

    assert "http://example.com/curated-content" in str(excinfo.value)


def test_validate_source_url_accepts_plain_http_when_explicitly_allowed() -> None:
    """AC-BI-010: `allow_insecure_http=True` is the documented opt-in."""
    url = "http://example.com/curated-content"

    assert validate_source_url(url, allow_insecure_http=True) == url


@pytest.mark.parametrize(
    "disallowed_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/curated-content",
        "not-a-url-at-all",
    ],
)
def test_validate_source_url_rejects_non_http_schemes(disallowed_url: str) -> None:
    """AC-BI-008: only http(s) schemes are ever accepted, e.g. never `file://`."""
    with pytest.raises(CuratedSourceConfigurationError) as excinfo:
        validate_source_url(disallowed_url, allow_insecure_http=True)

    assert disallowed_url in str(excinfo.value)


@pytest.mark.parametrize("empty_url", ["", "   ", "\t"])
def test_validate_source_url_rejects_empty_or_whitespace_only(empty_url: str) -> None:
    """Fails closed, mirroring `config._parse_host`'s "never widen to a fallback" style."""
    with pytest.raises(CuratedSourceConfigurationError):
        validate_source_url(empty_url, allow_insecure_http=True)
