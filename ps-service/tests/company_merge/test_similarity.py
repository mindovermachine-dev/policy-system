"""Tests for ps_service.company_merge.similarity."""

from __future__ import annotations

import pytest

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.similarity import cosine_similarity


def test_identical_vectors_have_similarity_one() -> None:
    vector = (1.0, 2.0, 3.0)
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)


def test_orthogonal_vectors_have_similarity_zero() -> None:
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_opposite_vectors_have_similarity_negative_one() -> None:
    assert cosine_similarity((1.0, 2.0, 3.0), (-1.0, -2.0, -3.0)) == pytest.approx(-1.0)


def test_zero_vector_raises_value_error() -> None:
    with pytest.raises(CompanyMergeValidationError, match="zero vector"):
        cosine_similarity((0.0, 0.0, 0.0), (1.0, 2.0, 3.0))


def test_zero_vector_as_second_argument_raises_value_error() -> None:
    with pytest.raises(CompanyMergeValidationError, match="zero vector"):
        cosine_similarity((1.0, 2.0, 3.0), (0.0, 0.0, 0.0))


def test_mismatched_length_vectors_raise_value_error_naming_both_lengths() -> None:
    with pytest.raises(CompanyMergeValidationError, match=r"\b3\b.*\b2\b"):
        cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0))
