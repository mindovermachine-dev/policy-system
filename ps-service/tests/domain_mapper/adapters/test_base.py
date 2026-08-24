"""Tests for ps_service.domain_mapper.adapters.base."""

from __future__ import annotations

from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
from ps_service.domain_mapper.falkordb_client import GraphHandle, GraphQueryResult
from ps_service.domain_mapper.models import ExtractionUnit


class _FakeGraphQueryResult:
    """A minimal structural implementation of `GraphQueryResult`."""

    @property
    def result_set(self) -> list[object]:
        return []


class _FakeGraphHandle:
    """A minimal structural implementation of `GraphHandle` — just enough
    to pass as this Protocol's `graph` argument without a `type: ignore`."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        return _FakeGraphQueryResult()


class _FakeAdapter:
    """A minimal structural implementation — proves `DomainMappingAdapter`
    is satisfied by duck typing (Protocol), not by inheritance."""

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        del graph  # unused by this fake — the Protocol imposes no naming convention of its own
        return (
            ExtractionUnit(
                citation_ref="Art. 13(1)",
                text="Manufacturers shall conduct a cybersecurity risk assessment.",
                article_number="13",
                paragraph_number="1",
                article_heading="Obligations of manufacturers",
            ),
        )


def test_fake_adapter_satisfies_domain_mapping_adapter_protocol() -> None:
    adapter: DomainMappingAdapter = _FakeAdapter()
    units = adapter.read_native_units(graph=_FakeGraphHandle())
    assert units[0].citation_ref == "Art. 13(1)"
