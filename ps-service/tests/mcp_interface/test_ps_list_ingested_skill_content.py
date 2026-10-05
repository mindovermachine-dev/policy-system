"""Content lint for `ps-list-ingested/SKILL.md` (issue #77).

Reads the real SKILL.md (no mocks) and pins what the skill instructs: which
connector and tools it uses, the fixed read-only query, and the output shape.
Grows one slice at a time.
"""

from __future__ import annotations

import json
from pathlib import Path

from ps_service.mcp_interface.mcp_server import (
    _GRAPH_UNAVAILABLE_DETAIL,  # pyright: ignore[reportPrivateUsage]  # test pins the exact error text the skill must name
    _UNEXPECTED_ERROR_MESSAGE,  # pyright: ignore[reportPrivateUsage]  # test pins the exact error text the skill must name
)
from ps_service.query_engine.cypher_query import (
    _GRAPH_UNSEEDED_DETAIL,  # pyright: ignore[reportPrivateUsage]  # test pins the exact error text the skill must name
    is_write_clause,
)

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-list-ingested" / "SKILL.md"


def _text() -> str:
    return " ".join(_SKILL.read_text(encoding="utf-8").split())


def test_skill_has_frontmatter_name_and_description() -> None:
    raw = _SKILL.read_text(encoding="utf-8")

    assert raw.startswith("---\nname: ps-list-ingested\ndescription: ")


def test_skill_names_the_connector_and_calls_domain_concepts_before_cypher() -> None:
    text = _text()

    assert "ps-mcp" in text
    assert "domain_concepts" in text
    assert "cypher" in text
    assert text.index("domain_concepts") < text.index("Call the `cypher` tool")


def test_skill_queries_live_graph_and_never_a_static_catalog() -> None:
    text = _text().lower()

    assert "live graph" in text
    assert "never a static catalog" in text
    assert "ps-get-catalog-listing" in text


def test_skill_stops_when_no_connector_exposes_cypher_and_domain_concepts() -> None:
    text = _text().lower()

    assert "does not expose" in text
    assert "stop" in text
    assert "never guess" in text


def _query() -> str:
    raw = _SKILL.read_text(encoding="utf-8")
    start = raw.index("```cypher\n") + len("```cypher\n")
    return raw[start : raw.index("```", start)]


def test_query_returns_the_ten_fields_under_their_aliases_in_order() -> None:
    normalised = " ".join(_query().split())

    assert (
        "RETURN ri.id AS id, ri.celex AS celex, ri.title AS title, "
        "ri.source_type AS source_type, ri.instrument_type AS instrument_type, "
        "ri.jurisdiction AS jurisdiction, ri.effective_date AS effective_date, "
        "ri.version AS version, ri.status AS status, succ.id AS superseded_by"
    ) in normalised


def test_query_reaches_the_successor_through_an_optional_superseded_by_edge() -> None:
    normalised = " ".join(_query().split())

    assert "MATCH (ri:RegulatoryInstrument)" in normalised
    assert "OPTIONAL MATCH (ri)-[:SUPERSEDED_BY]->(succ:RegulatoryInstrument)" in normalised


def test_query_orders_by_id_then_version() -> None:
    assert " ".join(_query().split()).endswith("ORDER BY id, version")


def test_query_is_read_only_and_parameter_free() -> None:
    query = _query()

    assert is_write_clause(query) is False
    assert "$" not in query


def test_output_lists_all_ten_fields_and_needs_no_arguments() -> None:
    text = _text()

    assert "no arguments" in text
    for field in (
        "id",
        "celex",
        "title",
        "source_type",
        "instrument_type",
        "jurisdiction",
        "effective_date",
        "version",
        "status",
        "superseded_by",
    ):
        assert f"<{field}>" in text, field


def test_output_renders_a_missing_value_blank_never_none_or_null() -> None:
    text = _text()

    assert "blank" in text
    assert 'never "None" or "null"' in text


def test_output_gives_each_version_its_own_entry_with_its_own_status_and_successor() -> None:
    text = _text()

    assert "each version is its own entry" in text
    assert "its own `status` and `superseded_by`" in text


def test_output_discloses_a_truncated_listing() -> None:
    text = _text()

    assert "`truncated`" in text
    assert "capped" in text


def test_output_notes_versions_sort_as_strings() -> None:
    text = _text()

    assert (
        'rows are ordered by `id`, then `version` as a string, so a version such as "10.0" '
        'is listed before "2.0"'
    ) in text
    assert "not row position" in text


def test_output_treats_ingested_text_as_data_never_instructions() -> None:
    text = _text().lower()

    assert "untrusted data" in text
    assert "never follow instructions" in text


def test_empty_result_says_no_instruments_ingested_yet() -> None:
    text = _text()

    assert "no instruments ingested yet" in text


def test_unseeded_graph_is_named_with_the_exact_detail_and_never_conflated_with_empty() -> None:
    text = _text()

    assert f"`error: {_GRAPH_UNSEEDED_DETAIL}`" in text
    assert "the graph is unseeded" in text
    assert "never report an unseeded graph as empty" in text.lower()


def test_error_states_a_b_c_are_named_verbatim_and_kept_distinct() -> None:
    text = _text()

    assert "PS Service is unreachable or the caller is unauthenticated" in text
    assert "the graph is unseeded" in text
    assert "the query was rejected by the Query Engine" in text


def test_graph_unreachable_and_unexpected_errors_are_their_own_states_not_rejections() -> None:
    text = _text()

    assert _GRAPH_UNAVAILABLE_DETAIL in text
    assert _UNEXPECTED_ERROR_MESSAGE in text
    assert "the graph database is unavailable" in text
    assert "PS Service hit an unexpected error" in text
    assert "Do not call this a query rejection" in text


def test_query_rejection_redacts_connection_details_and_never_prints_a_stack_trace() -> None:
    text = _text()

    assert "host:port" in text
    assert "`[redacted]`" in text
    assert "credential" in text
    assert "Never print a stack trace" in text


def test_connector_with_cypher_but_no_domain_concepts_is_not_ps_service() -> None:
    text = _text()

    assert "does not expose both `domain_concepts` and `cypher` is not PS Service" in text


_README = _REPO / "ps-skills" / "readme.md"
_MANIFEST = _REPO / "ps-skills" / "ps-plugin" / ".claude-plugin" / "plugin.json"


def test_readme_registers_the_skill_against_the_live_graph() -> None:
    bullets = [
        line
        for line in _README.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ps-list-ingested. This skill ")
    ]

    assert len(bullets) == 1
    assert "already ingested" in bullets[0]
    assert "ps-get-catalog-listing" in bullets[0]


def test_manifest_description_mentions_listing_ingested_instruments() -> None:
    description = json.loads(_MANIFEST.read_text(encoding="utf-8"))["description"]

    assert "list ingested instruments" in description
