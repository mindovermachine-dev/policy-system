"""`CellarEliAdapter`, the `IngestionAdapter` implementation for Cellar/ELI.

Composes `fetch.fetch_xhtml`/`fetch.fetch_rdf`, `metadata.extract_metadata` and
`structure.parse_structure` into one `fetch_regulatory_instrument_structure(identifier)` call
returning a `FetchedRegulatoryInstrumentStructure`.

`identifier` is a CELEX number. It is used unmodified as the fetch key and as `parse_structure`'s
prefix for structural node ids. The adapter never computes the final `{SHORT}-{VERSION}`
RegulatoryInstrument id; that is the pipeline's responsibility.

The module has no conditional on `identifier`'s value and no regulation-name literal, so every
regulation goes through the same three calls.
"""

from __future__ import annotations

from typing import Protocol

from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_rdf, fetch_xhtml
from ps_service.ingestion.adapters.cellar_eli.metadata import extract_metadata
from ps_service.ingestion.adapters.cellar_eli.structure import parse_structure
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
)


class _FetchCallable(Protocol):
    """The injection seam for the HTTP fetch step: take an identifier, return the raw bytes.

    Matches the call shape of `fetch_xhtml` and `fetch_rdf`, which are the defaults, so a
    different callable can be substituted without touching HTTP. The parameter is positional-only
    because those functions name theirs `celex`, not `identifier`, and every call here is
    positional.
    """

    def __call__(self, identifier: str, /) -> bytes: ...


class CellarEliAdapter:
    """`IngestionAdapter` implementation for the Cellar/ELI source.

    `fetch` (XHTML) and `fetch_rdf` (RDF notice) are independently injectable; the defaults are
    the real `fetch_xhtml` and `fetch_rdf`. Holds no other state.
    """

    def __init__(
        self, *, fetch: _FetchCallable = fetch_xhtml, fetch_rdf: _FetchCallable = fetch_rdf
    ) -> None:
        """Store the fetch callables.

        Args:
            fetch: Returns the XHTML bytes for a CELEX number (default `fetch_xhtml`).
            fetch_rdf: Returns the RDF bytes for a CELEX number (default `fetch_rdf`).
        """
        self._fetch = fetch
        self._fetch_rdf = fetch_rdf

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Fetch and parse one regulation by CELEX `identifier`.

        Args:
            identifier: CELEX number of the regulation, passed unmodified to the fetch
                callables and the parsers.

        Returns:
            The metadata plus the structural nodes and edges, as a
            `FetchedRegulatoryInstrumentStructure`.

        Raises:
            CellarFetchError: The XHTML or RDF fetch failed (`CellarNotFoundError` for a 404).
            CellarParseError: The metadata or structure could not be extracted.

        Side Effects:
            Two HTTP fetches through the injected callables.
        """
        xhtml = self._fetch(identifier)
        rdf = self._fetch_rdf(identifier)
        metadata = extract_metadata(xhtml, rdf, identifier)
        nodes, edges = parse_structure(xhtml, identifier)
        return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=nodes, edges=edges)

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        """Fetch one regulation's metadata by CELEX `identifier`, without parsing its structure.

        Makes the same fetch and `extract_metadata` calls as
        `fetch_regulatory_instrument_structure` but never calls `parse_structure`.

        Args:
            identifier: CELEX number of the regulation.

        Returns:
            The regulation's `RegulatoryInstrumentMetadata`.

        Raises:
            CellarFetchError: The XHTML or RDF fetch failed (`CellarNotFoundError` for a 404).
            CellarParseError: The metadata could not be extracted.

        Side Effects:
            Two HTTP fetches through the injected callables.
        """
        xhtml = self._fetch(identifier)
        rdf = self._fetch_rdf(identifier)
        return extract_metadata(xhtml, rdf, identifier)
