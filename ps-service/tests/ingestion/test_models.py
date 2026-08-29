"""Tests for ps_service.ingestion.models."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    IngestResult,
    ReachabilityCount,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)


def _metadata(**overrides: object) -> RegulatoryInstrumentMetadata:
    fields: dict[str, object] = {
        "title": "Regulation (EU) 2024/2847",
        "jurisdiction": "EU",
        "effective_date": "2027-12-11",
        "version": "1.0",
        "status": "active",
        "source_type": "external",
        "instrument_type": "regulation",
    }
    fields.update(overrides)
    return RegulatoryInstrumentMetadata.model_validate(
        {k: v for k, v in fields.items() if v is not None}
    )


def test_regulatory_instrument_metadata_mutation_raises() -> None:
    metadata = _metadata()
    with pytest.raises(ValidationError):
        metadata.title = "changed"  # type: ignore[misc]


def test_regulatory_instrument_metadata_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        _metadata(title="")


def test_regulatory_instrument_metadata_rejects_unparseable_effective_date() -> None:
    with pytest.raises(ValidationError):
        _metadata(effective_date="not-a-date")


def test_regulatory_instrument_metadata_parses_iso_date_string_into_date_object() -> None:
    metadata = _metadata(effective_date="2024-10-17")
    assert metadata.effective_date == date(2024, 10, 17)
    assert isinstance(metadata.effective_date, date)


def test_regulatory_instrument_metadata_rejects_instrument_type_outside_enum() -> None:
    with pytest.raises(ValidationError):
        _metadata(instrument_type="decision")


def test_regulatory_instrument_metadata_rejects_external_source_without_instrument_type() -> None:
    with pytest.raises(ValidationError):
        RegulatoryInstrumentMetadata.model_validate(
            {
                "title": "Regulation (EU) 2024/2847",
                "jurisdiction": "EU",
                "effective_date": "2027-12-11",
                "version": "1.0",
                "status": "active",
                "source_type": "external",
            }
        )


def test_regulatory_instrument_metadata_rejects_internal_source_with_instrument_type() -> None:
    with pytest.raises(ValidationError):
        _metadata(source_type="internal", instrument_type="regulation")


def test_regulatory_instrument_metadata_accepts_internal_source_without_instrument_type() -> None:
    metadata = _metadata(source_type="internal", instrument_type=None)
    assert metadata.instrument_type is None


def test_regulatory_instrument_metadata_accepts_national_transposition_instrument_type() -> None:
    metadata = _metadata(instrument_type="national_transposition")
    assert metadata.instrument_type == "national_transposition"


def test_structural_node_mutation_raises() -> None:
    node = StructuralNode(element_type="ARTICLE", id="CRA-1.0#art_1", properties={"order": 1})
    with pytest.raises(AttributeError):
        node.id = "changed"  # type: ignore[misc]


def test_structural_edge_mutation_raises() -> None:
    edge = StructuralEdge(
        parent_element_type="RegulatoryInstrument",
        parent_id="CRA-1.0",
        child_element_type="ARTICLE",
        child_id="CRA-1.0#art_1",
    )
    with pytest.raises(AttributeError):
        edge.parent_id = "changed"  # type: ignore[misc]


def test_fetched_regulatory_instrument_structure_mutation_raises() -> None:
    structure = FetchedRegulatoryInstrumentStructure(metadata=_metadata(), nodes=(), edges=())
    with pytest.raises(AttributeError):
        structure.nodes = ()  # type: ignore[misc]


def test_reachability_count_mutation_raises() -> None:
    count = ReachabilityCount(total=5, reachable=5)
    with pytest.raises(AttributeError):
        count.total = 1  # type: ignore[misc]


def test_ingest_result_mutation_raises() -> None:
    result = IngestResult(regulatory_instrument_id="CRA-1.0", run_id="run-1", counts={})
    with pytest.raises(AttributeError):
        result.run_id = "changed"  # type: ignore[misc]
