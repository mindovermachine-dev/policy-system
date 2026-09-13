"""Domain-specific exception types for `ps_service.domain_mapper`.

Mirrors `ps_service.ingestion.errors`'s shape (PLAN_REVIEWED.md §1): one
exception type per distinct failure boundary this component owns, never a
generic `Exception`/`ValueError` (L1 Error Handling, L2 Error Handling).
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

    Malformed/unparseable JSON, or a response missing required structure.
    Raised by `extraction.py`'s per-unit response parsing. Per
    PLAN_REVIEWED.md §5.2, this is caught and logged per-unit by
    `extract_roles_and_requirements`'s own loop, not left to propagate and
    abort the whole call — failure isolation, not a fail-fast infra
    boundary.

    `error_kind` is set (one of `ErrorKind`'s 9 values) on the 9 per-unit
    response-parsing raise sites in `domain_mapper/prompts.py`; `None` on
    every other raise site (a precondition/structural failure, not a
    per-unit LLM-response parse failure).
    """

    def __init__(self, message: str, *, error_kind: ErrorKind | None = None) -> None:
        """Record the message and, when known, the classified failure kind.

        Args:
            message: The human-readable failure description (may embed the
                raw payload/item that failed to parse).
            error_kind: One of `ErrorKind`'s 9 values, set only at the 9
                per-unit response-parsing raise sites in `prompts.py`;
                `None` on every other raise site.
        """
        super().__init__(message)
        self.error_kind = error_kind


class DomainMapperDerivationError(Exception):
    """An Obligation/Capability derivation step could not proceed.

    Malformed/unparseable LLM response for a mint-or-match decision, or a
    Requirement whose `role_id` bookkeeping property does not resolve to
    any Role node in the baseline graph (PLAN_REVIEWED.md §7.2's B3 fix
    (b)). Raised by `derivation.py`.
    """


class DomainMapperPersistenceError(Exception):
    """A FalkorDB write for the baseline graph could not be completed safely.

    E.g. a Requirement node about to be persisted references a Role node
    not present in the same write call (PLAN_REVIEWED.md §5.4's B3 fix
    (a)). Raised by `graph_writer.py`, before any `graph.query()` call is
    made for the offending write.
    """


class DomainMapperConfigurationError(Exception):
    """Domain Mapper's FalkorDB connection could not be established.

    From the resolved `ServiceConfig` — unreachable host/port, or the
    connection could not be validated. Raised by
    `falkordb_client.connect`/`connect_from_config`.
    """
