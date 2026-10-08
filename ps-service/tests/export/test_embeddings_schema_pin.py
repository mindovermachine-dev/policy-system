"""#199 AC-BI-006: embeddings backfill's label/property tables agree with the domain schema."""

from __future__ import annotations

from ps_service.domain_schema import enum_values, property_named
from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.export.embeddings import (
    _EMBEDDABLE_LABELS_BY_SOURCE_TYPE,  # pyright: ignore[reportPrivateUsage]
    _TEXT_PROPERTY_BY_LABEL,  # pyright: ignore[reportPrivateUsage]
)

_SCHEMA_LABELS = {node.label for node in DOMAIN_SCHEMA.nodes}


def test_text_property_labels_are_schema_labels_with_that_property() -> None:
    """#199 AC-BI-006: every mapped label is a schema label and owns its text property."""
    for label, text_property in _TEXT_PROPERTY_BY_LABEL.items():
        assert label in _SCHEMA_LABELS
        assert property_named(label, text_property).name == text_property


def test_embeddable_labels_are_schema_labels_with_a_text_property() -> None:
    """#199 AC-BI-006: every embeddable label is a schema label with a text-property entry."""
    for labels in _EMBEDDABLE_LABELS_BY_SOURCE_TYPE.values():
        for label in labels:
            assert label in _SCHEMA_LABELS
            assert label in _TEXT_PROPERTY_BY_LABEL


def test_embeddable_source_types_equal_schema_source_type_enum() -> None:
    """#199 AC-BI-006: the source-type keys equal the RegulatoryInstrument `source_type` enum."""
    assert set(_EMBEDDABLE_LABELS_BY_SOURCE_TYPE) == set(
        enum_values("RegulatoryInstrument", "source_type")
    )
