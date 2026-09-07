"""JSON Schema structural validation for the internal-regulation intake format (D7).

Validates only the structural allow-list layer: node ``label``/edge ``type``
enums, per-label required properties and their JSON types, and
``additionalProperties: false`` at every level -- closing D2's ``graph_name``
gap at the schema layer too (redundantly with the eventual Pydantic
``extra="forbid"`` parse in ``models.py``, not yet built in this slice).
Cross-node referential integrity (dangling edges) and cardinality rules
(exactly one ``HAS`` per Obligation) are ``persist.py``'s own semantic-
validation job, in a later slice (S2) -- JSON Schema validates one
document's shape, not graph invariants.
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

    Reads the packaged copy (D7: `ps_service/api/schemas/internal_regulation_intake_v1
    .schema.json`) via `importlib.resources`, never a relative filesystem path, so
    resolution is identical whether run from source or an installed wheel.
    """
    resource = resources.files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_RESOURCE_NAME)
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _describe_first_violation(
    validator: Draft202012Validator, raw: dict[str, object]
) -> str | None:
    """Return "{path}: {message}" for `raw`'s single most relevant schema violation, or `None`.

    Isolates every interaction with `jsonschema`'s own untyped/partially-typed
    stub to this one function (L2 Types Handling: untyped third-party
    libraries are handled by a commented `# pyright: ignore[...]` at their
    single boundary module, never a global downgrade). `best_match` (rather
    than the first/path-order error) is required because a node's own shape
    is dispatched per-label via `if`/`then` (D7): on a wrong-shaped node,
    naively picking an arbitrary branch error could surface an irrelevant
    message instead of, e.g., the specific missing `source_type` property.
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
    """Validate `raw` against the packaged JSON Schema's structural layer (D7).

    Raises `InternalSeedError` naming the single most relevant violation's
    document path and message when `raw` does not conform. No-ops on a
    conforming document. This is the first of the internal-seed adapter's two
    validation layers -- schema here, then referential integrity in
    `persist.py` (S2) -- called before any Pydantic parse, per D7/AC-BI-019.
    """
    validator = _load_validator()
    violation = _describe_first_violation(validator, raw)
    if violation is None:
        return
    raise InternalSeedError(f"internal-regulation seed document is invalid at '{violation}'")
