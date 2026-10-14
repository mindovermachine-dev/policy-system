"""Structural JSON Schema validation for the internal-regulation intake format.

Validates the structural allow-list layer only: node `label` and edge `type` enums, per-label
required properties and their JSON types, and `additionalProperties: false` at every level, which
also rejects a submitted `graph_name`. Cross-node referential integrity (dangling edges) and
cardinality rules (exactly one `HAS` per Obligation) are not checked here; they are the
`persist` module's job, because a JSON Schema validates one document's shape, not graph
invariants. The typed parse in `models.py` runs after this check.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import (
    ValidationError,
    best_match,  # pyright: ignore[reportUnknownVariableType]  # jsonschema: best_match's return type is inferred Unknown by its own stub
)

from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError

_SCHEMA_PACKAGE = "ps_service.api.schemas"
_SCHEMA_RESOURCE_NAME = "internal_regulation_intake_v1.schema.json"


@lru_cache(maxsize=1)
def _load_validator() -> Draft202012Validator:
    """Load and compile the packaged schema once; cached for the process lifetime.

    Reads `ps_service/api/schemas/internal_regulation_intake_v1.schema.json` through
    `importlib.resources`, never a relative filesystem path, so resolution is identical whether
    run from source or an installed wheel.
    """
    resource = resources.files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_RESOURCE_NAME)
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _describe_first_violation(
    validator: Draft202012Validator, raw: dict[str, object]
) -> str | None:
    """Return "{path}: {message}" for `raw`'s single most relevant schema violation, or `None`.

    Isolates every interaction with `jsonschema`'s partially typed stub to this one function, so
    the commented pyright suppressions stay at a single boundary. `best_match` rather
    than the first error is used because a node's shape is dispatched per label via `if`/`then`:
    on a wrong-shaped node, an arbitrary branch error could surface an irrelevant message instead
    of, for example, the specific missing `source_type` property.
    """
    errors = validator.iter_errors(  # pyright: ignore[reportUnknownMemberType]  # jsonschema: iter_errors' overloaded return type is partially unknown in the stub
        cast("Any", raw)  # pyright: ignore[reportArgumentType]  # jsonschema: stub's recursive _JsonParameter alias cannot express "arbitrary parsed JSON"
    )
    error = cast("ValidationError | None", best_match(errors))
    if error is None:
        return None
    path_segments = cast("list[object]", error.path)
    location = "/".join(str(segment) for segment in path_segments) or "<document root>"
    return f"{location}: {error.message}"


def validate_seed_document(raw: dict[str, object]) -> None:
    """Validate `raw` against the packaged JSON Schema's structural layer.

    This is the first of the adapter's validation layers; it runs before the Pydantic parse, and
    referential integrity is checked later by the `persist` module.

    Args:
        raw: The already-decoded intake document. Any shape is accepted as input; a document that
            is not the expected `nodes`/`edges` object fails validation.

    Returns:
        None. A conforming document passes silently.

    Raises:
        InternalSeedError: `raw` does not conform. The message names the document path and text
            of the single most relevant violation (`<document root>` when the path is empty).
    """
    validator = _load_validator()
    violation = _describe_first_violation(validator, raw)
    if violation is None:
        return
    raise InternalSeedError(f"internal-regulation seed document is invalid at '{violation}'")
