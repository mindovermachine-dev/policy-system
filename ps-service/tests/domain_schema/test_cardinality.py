"""Cardinality / Multiplicity parsing, rendering and flipping (AC-BI-002)."""

from __future__ import annotations

import pytest

from ps_service.domain_schema import DomainSchemaError
from ps_service.domain_schema.model import Cardinality, Multiplicity


@pytest.mark.parametrize("text", ["1 : 0..*", "1..* : 0..*", "0..1 : 0..1", "0..* : 1", "1 : 1..*"])
def test_cardinality_round_trips_through_text(text: str) -> None:
    """AC-BI-002: parse then str reproduces the doc's cardinality notation."""
    assert str(Cardinality.parse(text)) == text


def test_cardinality_parse_reads_both_sides() -> None:
    """AC-BI-002: left is sources-per-target, right is targets-per-source."""
    parsed = Cardinality.parse("1..* : 0..1")

    assert parsed.sources_per_target == Multiplicity(minimum=1, maximum=None)
    assert parsed.targets_per_source == Multiplicity(minimum=0, maximum=1)


def test_cardinality_flip_swaps_sides() -> None:
    """AC-BI-002: the inbound view of an edge is the flipped cardinality."""
    assert str(Cardinality.parse("1 : 0..*").flipped()) == "0..* : 1"
    assert str(Cardinality.parse("1..* : 0..*").flipped()) == "0..* : 1..*"


@pytest.mark.parametrize("text", ["", "1", "1 : ", "a : b", "1..2..3 : 1", "1 : 0..x"])
def test_cardinality_parse_rejects_malformed_text(text: str) -> None:
    """AC-BI-002: malformed cardinality text fails fast rather than guessing."""
    with pytest.raises(DomainSchemaError, match="cardinality"):
        Cardinality.parse(text)
