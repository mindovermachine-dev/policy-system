"""Cosine similarity -- a pure function.

New logic not present anywhere in shipped `ps_service` code
(PLAN_REVIEWED.md §0.4/§10 Increment 2). Used by
`dedup.find_best_semantic_match` to score an incoming node's embedding
against every existing canonical candidate's embedding.
"""

from __future__ import annotations

import math

from ps_service.company_merge.errors import CompanyMergeValidationError


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity between two equal-length embedding vectors, in `[-1.0, 1.0]`.

    Raises `CompanyMergeValidationError` if the vectors have different
    lengths (naming both lengths in the message) or if either vector is the
    zero vector (cosine similarity is undefined when a vector's magnitude is
    zero).
    """
    if len(a) != len(b):
        message = f"vectors must have the same length, got {len(a)} and {len(b)}"
        raise CompanyMergeValidationError(message)

    magnitude_a = math.sqrt(sum(component * component for component in a))
    magnitude_b = math.sqrt(sum(component * component for component in b))
    if magnitude_a == 0.0 or magnitude_b == 0.0:
        message = "cosine similarity is undefined for a zero vector"
        raise CompanyMergeValidationError(message)

    dot_product = sum(
        component_a * component_b for component_a, component_b in zip(a, b, strict=True)
    )
    return dot_product / (magnitude_a * magnitude_b)
