"""Tests for ps_service.ingestion.adapters.base."""

from __future__ import annotations

from datetime import date

from ps_service.ingestion.adapters.base import IngestionAdapter
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
)


class _FakeAdapter:
    """A minimal structural implementation — proves `IngestionAdapter` is
    satisfied by duck typing (Protocol), not by inheritance."""

    def fetch_regulatory_instrument_structure(self, identifier: str) -> FetchedRegulatoryInstrumentStructure:
        metadata = RegulatoryInstrumentMetadata(
            title="Fake Regulation",
            jurisdiction="EU",
            effective_date=date(2024, 1, 1),
            version="1.0",
            status="active",
            source_type="external",
            instrument_type="regulation",
        )
        return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=(), edges=())


def test_fake_adapter_satisfies_ingestion_adapter_protocol() -> None:
    adapter: IngestionAdapter = _FakeAdapter()
    structure = adapter.fetch_regulatory_instrument_structure("32024R2847")
    assert structure.metadata.title == "Fake Regulation"
