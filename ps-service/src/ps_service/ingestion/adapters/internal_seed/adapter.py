"""`InternalSeedIngestionAdapter` -- reads a customer-authored local-id JSON document (S2).

Unlike `cellar_eli.CellarEliAdapter` (fetches a regulation over HTTP by CELEX),
this adapter reads a document already resolved to a local filesystem path by
`ps_service.api.fixtures.resolve_fixture_path` -- `identifier` here is that
resolved path, as a string, never a network identifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.ingestion.adapters.internal_seed.schema import validate_seed_document


class InternalSeedIngestionAdapter:
    """Reads, JSON-Schema-validates, and Pydantic-parses one internal-regulation seed file."""

    def read_seed(self, identifier: str) -> InternalRegulationSeed:
        """Read the seed document at `identifier` and return its parsed form.

        Three checks run in order, each raising `InternalSeedError` (naming
        the specific problem, never a generic parse failure, AC-BI-019) on
        failure: the file is read and JSON-decoded; the raw document passes
        the packaged JSON Schema's structural check (D7 -- node label/edge
        type enums, required properties, no unrecognized top-level field);
        the document is then parsed into `InternalRegulationSeed`, which
        additionally enforces every field's own type (AC-BI-002's "unknown
        edge type" and AC-BI-003's "source_type must be internal" are both
        already caught by the schema step, since both are `enum`-constrained
        there -- the Pydantic parse is a second, statically-typed layer, not
        a redundant duplicate check written independently).

        Args:
            identifier: The resolved filesystem path to the seed document, as
                a string (never read from the operator's own machine -- see
                `ps_service.api.fixtures.resolve_fixture_path`).

        Returns:
            The parsed `InternalRegulationSeed`.

        Raises:
            InternalSeedError: The file could not be read, was not valid
                JSON, failed the packaged JSON Schema (D7), or failed to
                parse into `InternalRegulationSeed`.
        """
        path = Path(identifier)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InternalSeedError(
                f"could not read seed document at {identifier!r}: {exc}"
            ) from exc
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise InternalSeedError(
                f"seed document at {identifier!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise InternalSeedError(
                f"seed document at {identifier!r} must contain a JSON object at its root"
            )
        raw_document = cast("dict[str, object]", raw)
        validate_seed_document(raw_document)
        try:
            return InternalRegulationSeed.model_validate(raw_document)
        except ValidationError as exc:
            raise InternalSeedError(
                f"seed document at {identifier!r} failed to parse: {exc}"
            ) from exc
