"""The pluggable Ingestion Adapter interface.

CA doc Implementation Guidance: "Source-specific fetch/persist logic lives
behind an Ingestion Adapter interface... one concrete adapter per
regulatory source.".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ps_service.ingestion.models import FetchedRegulatoryInstrumentStructure


class IngestionAdapter(Protocol):
    """The one-method Ingestion Adapter Protocol.

    Matches the CA doc's single `FetchRegulatoryInstrumentStructure` action.

    `identifier` is a CELEX number for the Cellar/ELI adapter (CELEX-only
    per the user's decision — see PLAN_REVIEWED.md §1.2/§9, Open
    Question 2) — the Protocol itself imposes no format, so a future
    adapter (e.g. SOX/HIPAA) can define its own identifier shape without
    touching this interface.
    """

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Fetch and parse one regulatory instrument's structure by `identifier`."""
        ...
