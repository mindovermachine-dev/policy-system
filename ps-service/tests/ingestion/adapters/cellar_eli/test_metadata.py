"""Tests for ps_service.ingestion.adapters.cellar_eli.metadata.

Fixture XHTML snippets are small, hand-trimmed, but structurally realistic
against real Cellar/ELI markup (`eli-container`/`eli-main-title`/
`eli-subdivision`/`eli-title` classes, `art_NN`/`art_NN.tit_1` id
convention) — see `spikes/cellar1/LEARNINGS.md` for the real structure
these mirror.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from ps_service.ingestion.adapters.cellar_eli.metadata import (
    _BASE_ACT_VERSION,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _base_celex,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _find_effective_date,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _instrument_type_from_celex,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    _strip_namespace,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    extract_metadata,
)
from ps_service.ingestion.adapters.errors import CellarParseError

_TRANSPOSITION_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Directive (EU) 2022/2555 of the European Parliament
and of the Council</div>
<div class="eli-subdivision" id="art_41">
<div class="eli-title" id="art_41.tit_1">Article 41 Transposition</div>
<div id="041.001">1. By 17 October 2024, Member States shall adopt and publish the measures
necessary to comply with this Directive.</div>
</div>
</div>
</body>
</html>
"""

_ENTRY_INTO_FORCE_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Regulation (EU) 2024/2847 of the European Parliament
and of the Council</div>
<div class="eli-subdivision" id="art_71">
<div class="eli-title" id="art_71.tit_1">Article 71 Entry into force and application</div>
<div id="071.001">This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 11 December 2027.</div>
</div>
</div>
</body>
</html>
"""

_NO_DATE_HEADING_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Some Regulation</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Subject matter</div>
<div id="001.001">This Regulation establishes rules.</div>
</div>
</div>
</body>
</html>
"""


def _root(xhtml: bytes) -> ET.Element:
    root = ET.fromstring(xhtml)  # noqa: S314 — fixture XML authored in-repo, not untrusted
    _strip_namespace(root)
    return root


def test_find_effective_date_returns_transposition_date_when_transposition_heading_present() -> (
    None
):
    result = _find_effective_date(_root(_TRANSPOSITION_FIXTURE))

    assert result == date(2024, 10, 17)


def test_find_effective_date_returns_entry_into_force_date_when_no_transposition_heading() -> None:
    result = _find_effective_date(_root(_ENTRY_INTO_FORCE_FIXTURE))

    assert result == date(2027, 12, 11)


def test_find_effective_date_returns_none_when_neither_heading_present() -> None:
    result = _find_effective_date(_root(_NO_DATE_HEADING_FIXTURE))

    assert result is None


def test_extract_metadata_returns_title_from_eli_main_title() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert (
        metadata.title == "Directive (EU) 2022/2555 of the European Parliament and of the Council"
    )


def test_extract_metadata_sets_jurisdiction_status_and_source_type_constants() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert metadata.jurisdiction == "EU"
    assert metadata.status == "active"
    assert metadata.source_type == "external"


def test_extract_metadata_version_equals_base_act_version_constant() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert metadata.version == _BASE_ACT_VERSION


def test_extract_metadata_effective_date_is_a_real_date_object() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert metadata.effective_date == date(2024, 10, 17)
    assert isinstance(metadata.effective_date, date)


def test_extract_metadata_raises_cellar_parse_error_when_effective_date_unresolvable() -> None:
    with pytest.raises(CellarParseError):
        extract_metadata(_NO_DATE_HEADING_FIXTURE, "32999R9999")


def test_instrument_type_from_celex_maps_R_to_regulation() -> None:
    assert _instrument_type_from_celex("32016R0679") == "regulation"


def test_instrument_type_from_celex_maps_L_to_directive() -> None:
    assert _instrument_type_from_celex("32022L2555") == "directive"


def test_instrument_type_from_celex_raises_naming_unsupported_code() -> None:
    with pytest.raises(CellarParseError, match="'D'"):
        _instrument_type_from_celex("32014D0123")


def test_instrument_type_from_celex_raises_on_unparseable_identifier() -> None:
    with pytest.raises(CellarParseError):
        _instrument_type_from_celex("not-a-celex")


def test_extract_metadata_sets_instrument_type_regulation_for_R_celex() -> None:
    metadata = extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, "32024R2847")

    assert metadata.instrument_type == "regulation"


def test_extract_metadata_sets_instrument_type_directive_for_L_celex() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert metadata.instrument_type == "directive"


def test_extract_metadata_sets_celex_from_identifier() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE, "32022L2555")

    assert metadata.celex == "32022L2555"


def test_extract_metadata_raises_before_building_node_for_unsupported_celex_code() -> None:
    with pytest.raises(CellarParseError):
        extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, "32014D0123")


# --- Follow-on A1: consolidated-expression CELEX acceptance ---------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("32024R2847", "32024R2847"),
        ("32022L2555", "32022L2555"),
        ("02024R2847-20241120", "32024R2847"),
        ("02016R0679-20160504", "32016R0679"),
        ("02013R0575-20240709", "32013R0575"),
    ],
)
def test_base_celex_normalises_consolidated_and_passes_base_act_through(
    identifier: str, expected: str
) -> None:
    assert _base_celex(identifier) == expected


@pytest.mark.parametrize("identifier", ["garbage", "02024R2847", "not-a-celex"])
def test_base_celex_raises_on_unrecognised_identifier_form(identifier: str) -> None:
    with pytest.raises(CellarParseError):
        _base_celex(identifier)


def test_instrument_type_from_celex_accepts_consolidated_expression_form() -> None:
    assert _instrument_type_from_celex("02016R0679-20160504") == "regulation"


def test_extract_metadata_accepts_consolidated_celex_and_stores_base_act_celex() -> None:
    metadata = extract_metadata(_ENTRY_INTO_FORCE_FIXTURE, "02024R2847-20241120")

    assert metadata.celex == "32024R2847"
    assert metadata.instrument_type == "regulation"
    assert metadata.effective_date == date(2027, 12, 11)
