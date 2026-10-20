"""Tests for `ps_service.domain_mapper.role_names`: deterministic Role-name normalisation."""

from __future__ import annotations

import pytest

from ps_service.domain_mapper.identity import role_id
from ps_service.domain_mapper.role_names import normalise_role_name, role_name_key


@pytest.mark.parametrize(
    "spelling",
    ["Manufacturer", "manufacturers", "The manufacturer", " Manufacturers ", "A  MANUFACTURER"],
)
def test_spellings_of_one_actor_share_one_key(spelling: str) -> None:
    assert role_name_key(spelling) == "manufacturer"


def test_the_display_name_keeps_the_original_casing_without_article_or_plural() -> None:
    assert normalise_role_name("The Manufacturer") == "Manufacturer"
    assert normalise_role_name("  Manufacturers ") == "Manufacturer"
    assert normalise_role_name("an importer") == "importer"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorities", "Authority"),
        ("Market surveillance authorities", "Market surveillance authority"),
        ("Businesses", "Business"),
        ("Business", "Business"),
        ("Status", "Status"),
        ("Analysis", "Analysis"),
        ("Notified bodies", "Notified body"),
        ("Gas", "Gas"),
        ("DNS", "DNS"),
        ("CSIRTs", "CSIRT"),
    ],
)
def test_only_the_last_word_is_singularised_by_the_explicit_rules(raw: str, expected: str) -> None:
    assert normalise_role_name(raw) == expected


def test_a_compound_name_is_not_split() -> None:
    assert normalise_role_name("Importer or distributor") == "Importer or distributor"


def test_only_the_last_word_changes_in_a_multi_word_name() -> None:
    assert (
        normalise_role_name("Operators of essential services") == "Operators of essential service"
    )


def test_only_one_leading_article_is_dropped_and_a_lone_article_is_kept() -> None:
    assert normalise_role_name("The the manufacturer") == "the manufacturer"
    assert normalise_role_name("The") == "The"
    assert normalise_role_name("A") == "A"


def test_internal_whitespace_collapses() -> None:
    assert (
        normalise_role_name("Market   surveillance \n authority") == "Market surveillance authority"
    )


@pytest.mark.parametrize(
    "raw", ["Manufacturers", "The Authorities", "Businesses", "Notified bodies", "Branches"]
)
def test_normalisation_is_idempotent(raw: str) -> None:
    once = normalise_role_name(raw)

    assert normalise_role_name(once) == once


def test_the_role_id_function_itself_is_unchanged() -> None:
    assert role_id("Manufacturer", "CRA-1.0") != role_id("Manufacturers", "CRA-1.0")
    assert role_id("Manufacturer", "CRA-1.0") == role_id("MANUFACTURER", "CRA-1.0")
