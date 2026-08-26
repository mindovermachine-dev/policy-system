"""Domain-specific exception types for `ps_service.company_merge`.

Mirrors `ps_service.domain_mapper.errors`'s shape (PLAN_REVIEWED.md §1): one
exception type per distinct failure boundary this component owns, never a
generic `Exception`/`ValueError` (L1 Error Handling, L2 Error Handling).
"""

from __future__ import annotations


class CompanyMergeConfigurationError(Exception):
    """`merge_baseline_graph` was called without a resolved similarity
    threshold -- `similarity_threshold is None`, meaning
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` was never set/resolved via
    `ServiceConfig` (PLAN_REVIEWED.md §7 step 0, §8's B1 fix).

    Raised by `merge.py`, before any graph call of any kind is made.
    """


class CompanyMergePersistenceError(Exception):
    """A FalkorDB write for the single-tenant graph could not be completed
    safely -- e.g. an edge rewiring write references a canonical id with no
    corresponding `CanonicalResolution` (PLAN_REVIEWED.md §6).

    Raised by `graph_writer.py`, before any `graph.query()` call is made
    for the offending write.
    """


class CompanyMergeValidationError(Exception):
    """An input to a pure `company_merge` computation is malformed -- e.g.
    `similarity.cosine_similarity` was given vectors of mismatched length,
    or a zero-magnitude vector for which cosine similarity is undefined.

    Raised by `similarity.py`, before any similarity score is computed.
    """
