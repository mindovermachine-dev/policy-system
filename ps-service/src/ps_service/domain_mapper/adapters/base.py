"""The pluggable Domain Mapping Adapter interface.

A Domain Mapping Adapter reads a regulatory source's native structural graph and yields the
extraction units the Domain Mapper sends to the LLM. There is one adapter per regulatory
source, paired 1:1 with that source's Ingestion Adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.domain_mapper.models import ExtractionUnit


class DomainMappingAdapter(Protocol):
    """The one-method contract every Domain Mapping Adapter implements.

    Reads a regulation's native structural graph (already selected via
    `graph` — the caller resolves `{short}_native`, this Protocol imposes
    no naming convention of its own) and returns the ordered sequence of
    extraction units. Paired 1:1 with an Ingestion Adapter — a Domain
    Mapping Adapter's expected input shape must track its paired Ingestion
    Adapter's output shape exactly.
    """

    def read_native_units(
        self, graph: GraphHandle, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        """Return the ordered extraction units of ONE instrument version read from `graph`.

        `{short}_native` holds every ingested version of a regulation side by side, so the
        read is anchored on `regulatory_instrument_id` and reaches only that node's subtree.

        Args:
            graph: Read handle on the regulation's native graph (`{short}_native`).
            regulatory_instrument_id: Id of the `RegulatoryInstrument` node whose subtree is read.

        Returns:
            The extraction units in document order; empty when the subtree holds no readable
            content.

        Raises:
            DomainMapperExtractionError: If a node in the subtree has a citation label the
                adapter cannot parse.
        """
        ...
