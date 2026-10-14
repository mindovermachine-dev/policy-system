"""`InternalSeedIngestionAdapter`: parses a customer-authored local-id JSON document.

Unlike `cellar_eli.CellarEliAdapter`, which fetches a regulation over HTTP by CELEX, this adapter
parses a document the caller has already read and JSON-decoded (the `POST /ingestions` request
body). The `document` argument is that parsed `dict`, never a filesystem path or network
identifier.
"""

from __future__ import annotations

from pydantic import ValidationError

from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.ingestion.adapters.internal_seed.schema import validate_seed_document


class InternalSeedIngestionAdapter:
    """Validates one internal-regulation seed document and parses it into typed models.

    The adapter is stateless: each `parse_seed` call is independent and performs no I/O.
    """

    def parse_seed(self, document: dict[str, object]) -> InternalRegulationSeed:
        """Validate `document` and return it as an `InternalRegulationSeed`.

        Two checks run in order, so a failure names the specific problem rather than a generic
        parse failure. First the packaged JSON Schema checks structure: node label and edge type
        enums, required properties per label, and no unrecognized top-level field. Then Pydantic
        parses the document, which additionally enforces every field's own type. The schema
        already rejects an unknown edge type or a non-internal `source_type` (both are enums
        there); the Pydantic parse is a second, statically typed layer.

        Args:
            document: The already-parsed intake document (the request body's `content` field).
                Must be a JSON object with `nodes` and `edges` arrays; never read from disk by
                this adapter.

        Returns:
            The parsed `InternalRegulationSeed`; it is never `None`.

        Raises:
            InternalSeedError: The document failed the packaged JSON Schema, or failed to parse
                into `InternalRegulationSeed`.
        """
        validate_seed_document(document)
        try:
            return InternalRegulationSeed.model_validate(document)
        except ValidationError as exc:
            raise InternalSeedError(f"seed document failed to parse: {exc}") from exc
