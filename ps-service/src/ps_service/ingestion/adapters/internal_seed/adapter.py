"""`InternalSeedIngestionAdapter` -- parses a customer-authored local-id JSON document (S2).

Unlike `cellar_eli.CellarEliAdapter` (fetches a regulation over HTTP by CELEX),
this adapter parses a document already read and JSON-decoded by the caller
(the `POST /ingestions` request body, issue #91) -- `document` here is that
already-parsed `dict`, never a filesystem path or network identifier.
"""

from __future__ import annotations

from pydantic import ValidationError

from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.ingestion.adapters.internal_seed.schema import validate_seed_document


class InternalSeedIngestionAdapter:
    """JSON-Schema-validates and Pydantic-parses one internal-regulation seed document."""

    def parse_seed(self, document: dict[str, object]) -> InternalRegulationSeed:
        """Parse `document` and return its parsed form.

        Two checks run in order, each raising `InternalSeedError` (naming
        the specific problem, never a generic parse failure, AC-BI-019) on
        failure: the document passes the packaged JSON Schema's structural
        check (D7 -- node label/edge type enums, required properties, no
        unrecognized top-level field); the document is then parsed into
        `InternalRegulationSeed`, which additionally enforces every field's
        own type (AC-BI-002's "unknown edge type" and AC-BI-003's
        "source_type must be internal" are both already caught by the schema
        step, since both are `enum`-constrained there -- the Pydantic parse
        is a second, statically-typed layer, not a redundant duplicate check
        written independently).

        Args:
            document: The already-parsed intake document (the request body's
                `content` field, issue #91) -- never read from disk by this
                adapter.

        Returns:
            The parsed `InternalRegulationSeed`.

        Raises:
            InternalSeedError: The document failed the packaged JSON Schema
                (D7), or failed to parse into `InternalRegulationSeed`.
        """
        validate_seed_document(document)
        try:
            return InternalRegulationSeed.model_validate(document)
        except ValidationError as exc:
            raise InternalSeedError(f"seed document failed to parse: {exc}") from exc
