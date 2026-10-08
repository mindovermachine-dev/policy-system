"""Marker region engine: only the bytes inside generated regions are rewritten (AC-BI-007)."""

from __future__ import annotations

import pytest

from ps_service.domain_schema.errors import DomainSchemaError
from ps_service.domain_schema.render_doc import (
    begin_marker,
    end_marker,
    find_region_ids,
    replace_regions,
)

_DOC = (
    "# Title\n\nhand written before\n\n"
    f"{begin_marker('edge-catalog')}\n\nold table\n\n{end_marker('edge-catalog')}\n\n"
    "hand written between\n\n"
    f"{begin_marker('properties:Role')}\n\nold role\n\n{end_marker('properties:Role')}\n\n"
    "hand written after\n"
)
_RENDERED = {"edge-catalog": "| new |\n", "properties:Role": "| role |\n"}


def _outside(text: str) -> list[str]:
    """Return the text segments outside every BEGIN..END pair."""
    segments: list[str] = []
    rest = text
    while "<!-- BEGIN GENERATED" in rest:
        before, _, after = rest.partition("<!-- BEGIN GENERATED")
        segments.append(before)
        _, _, rest = after.partition("<!-- END GENERATED")
        rest = rest.partition("-->")[2]
    segments.append(rest)
    return segments


def test_replaces_only_the_region_bodies_and_keeps_outside_bytes() -> None:
    """AC-BI-007: text outside the markers is returned byte-unchanged."""
    updated = replace_regions(_DOC, _RENDERED)

    assert "| new |" in updated
    assert "old table" not in updated
    assert _outside(updated) == _outside(_DOC)


def test_replacement_is_idempotent() -> None:
    once = replace_regions(_DOC, _RENDERED)

    assert replace_regions(once, _RENDERED) == once


def test_marker_text_says_generated_and_do_not_edit() -> None:
    """AC-BI-018: the BEGIN marker names the source and the regenerate command."""
    marker = begin_marker("properties:Role")

    assert "Generated from ps_service.domain_schema" in marker
    assert "do not edit by hand" in marker
    assert "write-docs" in marker
    assert "--" not in marker.removeprefix("<!--").removesuffix("-->")


def test_find_region_ids_lists_regions_in_document_order() -> None:
    assert find_region_ids(_DOC) == ("edge-catalog", "properties:Role")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"{begin_marker('edge-catalog')}\n\nx\n", "edge-catalog"),
        (f"{end_marker('edge-catalog')}\n\n{begin_marker('edge-catalog')}\n", "edge-catalog"),
        (
            (
                f"{begin_marker('edge-catalog')}\n{end_marker('edge-catalog')}\n"
                f"{begin_marker('edge-catalog')}\n{end_marker('edge-catalog')}\n"
            ),
            "edge-catalog",
        ),
        (
            (
                f"{begin_marker('edge-catalog')}\n{begin_marker('properties:Role')}\n"
                f"{end_marker('properties:Role')}\n{end_marker('edge-catalog')}\n"
            ),
            "properties:Role",
        ),
        (f"{begin_marker('unknown-region')}\n{end_marker('unknown-region')}\n", "unknown-region"),
        (
            f"{begin_marker('edge-catalog')}\n{end_marker('properties:Role')}\n",
            "properties:Role",
        ),
    ],
    ids=["missing-end", "end-before-begin", "duplicate-id", "nested", "unknown-id", "mismatch"],
)
def test_malformed_regions_raise_naming_the_offender(text: str, message: str) -> None:
    with pytest.raises(DomainSchemaError, match=message):
        replace_regions(text, _RENDERED)


def test_crlf_outside_is_preserved_and_inside_is_normalised_to_lf() -> None:
    crlf = _DOC.replace("\n", "\r\n")

    updated = replace_regions(crlf, _RENDERED)

    assert _outside(updated) == _outside(crlf)
    inside = updated.split(begin_marker("edge-catalog"))[1].split(end_marker("edge-catalog"))[0]
    assert "\r" not in inside.removeprefix("\r\n")
    assert "| new |\n" in inside
