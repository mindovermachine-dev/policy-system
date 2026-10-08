"""#199 AC-BI-005: ingestion's hand-copied enum Literals equal the domain schema."""

from __future__ import annotations

from typing import get_args

import pytest

from ps_service.domain_schema import enum_values
from ps_service.ingestion.models import InstrumentType, RegulatoryInstrumentStatus, SourceType


@pytest.mark.parametrize(
    ("alias", "prop"),
    [
        (RegulatoryInstrumentStatus, "status"),
        (SourceType, "source_type"),
        (InstrumentType, "instrument_type"),
    ],
)
def test_ingestion_literal_equals_schema_enum(alias: object, prop: str) -> None:
    """#199 AC-BI-005: each ingestion Literal alias has exactly the schema enum's values.

    Args:
        alias: The `Literal[...]` alias declared in `ps_service.ingestion.models`.
        prop: The `RegulatoryInstrument` enum property it mirrors.
    """
    assert set(get_args(alias)) == set(enum_values("RegulatoryInstrument", prop))
