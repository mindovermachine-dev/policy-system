"""`CellarEliAdapter` — the `IngestionAdapter` implementation for Cellar/ELI.

Composes `fetch.py::fetch_xhtml`/`fetch_rdf` (Increment 2; `fetch_rdf` added
by issue #62), `metadata.py::extract_metadata` (Increment 3), and
`structure.py::parse_structure` (Increment 4) into one
`fetch_regulatory_instrument_structure(identifier) -> FetchedRegulatoryInstrumentStructure`
call — the CA doc's single `FetchRegulatoryInstrumentStructure` action.

`identifier` is a CELEX number (CELEX-only per the user's decision, see
PLAN_REVIEWED.md §1.2/§9 Open Question 2) and is used, unmodified, both as
the fetch key and as `parse_structure`'s `regulatory_instrument_id` prefix for
structural node ids — the adapter never computes or sees the final
`{SHORT}-{VERSION}` RegulatoryInstrument id, which is `pipeline.py`'s responsibility
(PLAN_REVIEWED.md §3), not the adapter's.

This is the concrete proof-of-pluggability point for AC-001/AC-006 at the
adapter level: this module's own source contains no conditional or
comparison on `identifier`'s value, and no regulation-name literal
anywhere — every regulation this adapter ever ingests goes through the
exact same three calls below.
"""

from __future__ import annotations

from typing import Protocol

from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_rdf, fetch_xhtml
from ps_service.ingestion.adapters.cellar_eli.metadata import extract_metadata
from ps_service.ingestion.adapters.cellar_eli.structure import parse_structure
from ps_service.ingestion.models import FetchedRegulatoryInstrumentStructure


class _FetchCallable(Protocol):
    """The DI seam for the HTTP fetch step.

    L2: business logic must not construct its own infrastructure clients
    inline. Matches `fetch_xhtml`'s (and `fetch_rdf`'s — identical call
    shape) call shape exactly, so the real `fetch_xhtml`/`fetch_rdf` are the
    defaults while a test can substitute a fake without touching HTTP.
    Positional-only parameter (mirrors `fetch.py::CellarTransport`'s same
    fix): `fetch_xhtml`/`fetch_rdf`'s own parameter is named `celex`, not
    `identifier`, and every call site here calls positionally, so a name
    mismatch must not break structural assignability.
    """

    def __call__(self, identifier: str, /) -> bytes: ...


class CellarEliAdapter:
    """`IngestionAdapter` implementation for the Cellar/ELI source.

    `fetch`/`fetch_rdf` are independently injectable (defaults: the real
    `fetch_xhtml`/`fetch_rdf`) so tests can substitute fakes per L2 DI,
    without monkeypatching HTTP transport.
    """

    def __init__(
        self, *, fetch: _FetchCallable = fetch_xhtml, fetch_rdf: _FetchCallable = fetch_rdf
    ) -> None:
        """Store the injected `fetch`/`fetch_rdf` callables (defaults: the real ones)."""
        self._fetch = fetch
        self._fetch_rdf = fetch_rdf

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Fetch and parse one regulation by CELEX `identifier`.

        Lets `CellarFetchError` (from `fetch`/`fetch_rdf`) and
        `CellarParseError` (from `extract_metadata`/`parse_structure`)
        propagate unchanged — both are already the correct adapter-level
        exception type for their failure, so this method never catches or
        re-wraps them.
        """
        xhtml = self._fetch(identifier)
        rdf = self._fetch_rdf(identifier)
        metadata = extract_metadata(xhtml, rdf, identifier)
        nodes, edges = parse_structure(xhtml, identifier)
        return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=nodes, edges=edges)
