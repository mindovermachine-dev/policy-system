"""Domain-specific exception types for `ps_service.domain_mapper`.

Mirrors `ps_service.ingestion.errors`'s shape: one exception type per distinct failure
boundary this component owns, never a generic `Exception` or `ValueError`.
"""

from __future__ import annotations

from typing import Literal

type ErrorKind = Literal[
    "invalid_json",
    "missing_requirements_key",
    "non_list_requirements",
    "non_object_item",
    "invalid_requirement_item",
    "invalid_definitions_json",
    "missing_terms_key",
    "non_list_terms",
    "invalid_defined_term_item",
]


class DomainMapperExtractionError(Exception):
    """A unit's LLM-driven extraction call did not yield valid `RequirementCandidate`s.

    Raised for malformed or unparseable JSON, or a response missing required structure, and
    for a native-graph row with an unexpected citation shape. The extraction loop in
    `extract_roles_and_requirements` catches and logs it per unit instead of letting it abort
    the whole call, so one bad unit does not stop the others.

    Attributes:
        error_kind: One of the `ErrorKind` values when the failure is a classified per-unit
            response-parsing failure (raised in `domain_mapper/prompts.py`); `None` for every
            other raise site (a precondition or structural failure).
    """

    def __init__(self, message: str, *, error_kind: ErrorKind | None = None) -> None:
        """Record the message and, when known, the classified failure kind.

        Args:
            message: Human-readable failure description; may embed the raw payload or item
                that failed to parse.
            error_kind: One of the `ErrorKind` values for a classified response-parsing
                failure; `None` (the default) for any other failure.
        """
        super().__init__(message)
        self.error_kind = error_kind


class DomainMapperDerivationError(Exception):
    """An Obligation/Capability derivation step could not proceed.

    Raised for a malformed or unparseable LLM response to a mint-or-match decision, or for a
    Requirement whose `role_id` bookkeeping property does not resolve to any Role node in the
    baseline graph. Raised by `derivation.py` and `prompts.py`; the caller should treat the
    derivation as failed.
    """


class DomainMapperPersistenceError(Exception):
    """A baseline-graph write could not be completed safely.

    Raised by `graph_writer.py`, before anything is submitted to the Graph Write Gateway, if a
    Requirement node about to be persisted references a Role node not present in the same call,
    or a node or edge carries a property the gateway cannot store (a reserved key, a
    non-finite number). Also raised, with the message `FalkorDB write failed: <sanitized text>`
    and the gateway error chained, when the gateway refuses a group or cannot reach its stores,
    when a group is committed but not yet applied, or when the baseline graph could not be
    read.
    """


class DomainMapperConfigurationError(Exception):
    """Domain Mapper's FalkorDB connection could not be established.

    Raised by `falkordb_client.check_connectivity` when the host and port taken from the
    resolved `ServiceConfig` are unreachable or the connection cannot list its graphs. The
    underlying cause is chained.
    """
