"""The pluggable Ingestion Adapter interface.

Source-specific fetch and parse logic lives behind this interface, one concrete adapter per
regulatory source, so the ingestion core stays source-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ps_service.ingestion.models import FetchedRegulatoryInstrumentStructure


class IngestionAdapter(Protocol):
    """The one-method Ingestion Adapter Protocol.

    An adapter fetches and parses one regulatory instrument in a single action.

    `identifier` is a CELEX number for the Cellar/ELI adapter. The Protocol imposes no format,
    so another adapter can define its own identifier shape without touching this interface.
    """

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Fetch and parse one regulatory instrument by source `identifier`.

        Args:
            identifier: Source-specific key of the instrument (a CELEX number for Cellar/ELI).

        Returns:
            The instrument's metadata plus its native structural nodes and edges.

        Raises:
            Exception: Adapter-specific fetch or parse errors propagate unchanged (for
                Cellar/ELI, `CellarFetchError` and `CellarParseError`).
        """
        ...
