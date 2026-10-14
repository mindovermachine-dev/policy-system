"""Cosine similarity -- a pure function.

`dedup.find_best_semantic_match` uses it to score an incoming node's embedding against every
existing canonical candidate's embedding.
"""

from __future__ import annotations

import math

from ps_service.company_merge.errors import CompanyMergeValidationError


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity between two equal-length embedding vectors.

    Args:
        a: First embedding vector; non-zero.
        b: Second embedding vector; non-zero and the same length as `a`.

    Returns:
        The cosine of the angle between the vectors, in `[-1.0, 1.0]`.

    Raises:
        CompanyMergeValidationError: The vectors have different lengths (both lengths are named
            in the message) or either is the zero vector, for which cosine similarity is
            undefined.
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
