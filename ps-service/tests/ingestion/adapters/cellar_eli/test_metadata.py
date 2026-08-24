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
    _BASE_ACT_VERSION,
    _find_effective_date,
    _strip_namespace,
    extract_metadata,
)
from ps_service.ingestion.adapters.errors import CellarParseError

_TRANSPOSITION_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Directive (EU) 2022/2555 of the European Parliament and of the Council</div>
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
<div class="eli-main-title">Regulation (EU) 2024/2847 of the European Parliament and of the Council</div>
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
    root = ET.fromstring(xhtml)
    _strip_namespace(root)
    return root


def test_find_effective_date_returns_transposition_date_when_transposition_heading_present() -> None:
    result = _find_effective_date(_root(_TRANSPOSITION_FIXTURE))

    assert result == date(2024, 10, 17)


def test_find_effective_date_returns_entry_into_force_date_when_no_transposition_heading() -> None:
    result = _find_effective_date(_root(_ENTRY_INTO_FORCE_FIXTURE))

    assert result == date(2027, 12, 11)


def test_find_effective_date_returns_none_when_neither_heading_present() -> None:
    result = _find_effective_date(_root(_NO_DATE_HEADING_FIXTURE))

    assert result is None


def test_extract_metadata_returns_title_from_eli_main_title() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE)

    assert metadata.title == "Directive (EU) 2022/2555 of the European Parliament and of the Council"


def test_extract_metadata_sets_jurisdiction_status_and_source_type_constants() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE)

    assert metadata.jurisdiction == "EU"
    assert metadata.status == "active"
    assert metadata.source_type == "external"


def test_extract_metadata_version_equals_base_act_version_constant() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE)

    assert metadata.version == _BASE_ACT_VERSION


def test_extract_metadata_effective_date_is_a_real_date_object() -> None:
    metadata = extract_metadata(_TRANSPOSITION_FIXTURE)

    assert metadata.effective_date == date(2024, 10, 17)
    assert isinstance(metadata.effective_date, date)


def test_extract_metadata_raises_cellar_parse_error_when_effective_date_unresolvable() -> None:
    with pytest.raises(CellarParseError):
        extract_metadata(_NO_DATE_HEADING_FIXTURE)
