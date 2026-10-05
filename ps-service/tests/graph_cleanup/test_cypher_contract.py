"""Lint of every graph-cleanup writer statement against the CHANGES.md I1-I5 contract.

Default-suite stand-in for the live proof: it cannot detect a FalkorDB syntax error but
does catch a statement that drifts from the guard-first, one-row, parameterised shape.
"""

from __future__ import annotations

import re

import pytest

from ps_service.graph_cleanup.graph_writer import (
    MERGE_CAPABILITIES_QUERY,
    MERGE_OBLIGATIONS_QUERY,
    UNMERGE_CAPABILITY_QUERY,
    UNMERGE_OBLIGATION_QUERY,
)

_WRITE_KEYWORD = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|FOREACH)\b")
_STATEMENTS = {
    "merge_capabilities": MERGE_CAPABILITIES_QUERY,
    "merge_obligations": MERGE_OBLIGATIONS_QUERY,
    "unmerge_capability": UNMERGE_CAPABILITY_QUERY,
    "unmerge_obligation": UNMERGE_OBLIGATION_QUERY,
}


def _guard_index(text: str) -> int:
    """Index of the final `WITH ... WHERE` guard: the last top-level WHERE clause."""
    return text.rindex("\nWHERE ")


@pytest.mark.parametrize("name", sorted(_STATEMENTS))
def test_no_write_keyword_precedes_the_guard(name: str) -> None:
    text = _STATEMENTS[name]

    assert not _WRITE_KEYWORD.search(text[: _guard_index(text)])
    assert _WRITE_KEYWORD.search(text[_guard_index(text) :])


@pytest.mark.parametrize("name", sorted(_STATEMENTS))
def test_every_optional_match_is_followed_by_a_collecting_with(name: str) -> None:
    text = _STATEMENTS[name]
    clauses = re.split(
        r"\n(?=OPTIONAL MATCH|WITH |WHERE |FOREACH|MATCH |SET |MERGE |RETURN )", text
    )

    for index, clause in enumerate(clauses):
        if clause.startswith("OPTIONAL MATCH"):
            following = clauses[index + 1]
            assert following.startswith("WITH ")
            assert "collect(DISTINCT" in following


@pytest.mark.parametrize("name", sorted(_STATEMENTS))
def test_statement_is_one_query_returning_one_row_and_uses_no_inline_literals_for_ids(
    name: str,
) -> None:
    text = _STATEMENTS[name]

    assert text.count("RETURN") == 1
    assert ";" not in text
    assert "$survivor_id" in text
    assert "$absorbed_id" in text
    assert "'cap_" not in text
    assert "'obl_" not in text


def test_every_parameter_of_the_statement_is_supplied_by_the_writer() -> None:
    from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedMergeGraph
    from ps_service.graph_cleanup.graph_writer import merge_capabilities
    from ps_service.graph_cleanup.models import ExpectedCounts

    graph = ScriptedMergeGraph()
    merge_capabilities(
        graph,
        survivor_id=SURVIVOR,
        absorbed_id=ABSORBED,
        expected=ExpectedCounts(requires=0, covers=0, mitigated=0),
    )

    used = set(re.findall(r"\$(\w+)", MERGE_CAPABILITIES_QUERY))
    params = graph.queries[0][1]
    assert params is not None
    assert used == set(params)


def test_every_parameter_of_the_obligation_statement_is_supplied_by_the_writer() -> None:
    from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ScriptedObligationGraph
    from ps_service.graph_cleanup.graph_writer import merge_obligations
    from ps_service.graph_cleanup.models import ObligationExpectedCounts

    graph = ScriptedObligationGraph()
    merge_obligations(
        graph,
        survivor_id=OBL_SURVIVOR,
        absorbed_id=OBL_ABSORBED,
        expected=ObligationExpectedCounts(satisfied=0, requires=0),
    )

    used = set(re.findall(r"\$(\w+)", MERGE_OBLIGATIONS_QUERY))
    params = graph.queries[0][1]
    assert params is not None
    assert used == set(params)


def test_every_parameter_of_the_unmerge_statement_is_supplied_by_the_writer() -> None:
    from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedUnmergeGraph
    from ps_service.graph_cleanup.graph_writer import unmerge_capability
    from ps_service.graph_cleanup.models import CapabilityUnmergeWrite

    graph = ScriptedUnmergeGraph()
    unmerge_capability(
        graph,
        absorbed_id=ABSORBED,
        survivor_id=SURVIVOR,
        write=CapabilityUnmergeWrite(
            restore_requires_ids=(),
            remove_requires_ids=(),
            restore_covers_ids=(),
            remove_covers_ids=(),
            restore_mitigated_ids=(),
            remove_mitigated_ids=(),
            restore_policy_id=None,
            remove_policy_edge=False,
        ),
    )

    used = set(re.findall(r"\$(\w+)", UNMERGE_CAPABILITY_QUERY))
    params = graph.queries[0][1]
    assert params is not None
    assert used == set(params)


def test_every_parameter_of_the_obligation_unmerge_statement_is_supplied_by_the_writer() -> None:
    from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ScriptedObligationUnmergeGraph
    from ps_service.graph_cleanup.graph_writer import unmerge_obligation
    from ps_service.graph_cleanup.models import ObligationUnmergeWrite

    graph = ScriptedObligationUnmergeGraph()
    unmerge_obligation(
        graph,
        absorbed_id=OBL_ABSORBED,
        survivor_id=OBL_SURVIVOR,
        write=ObligationUnmergeWrite(
            role_id="role_1", satisfied_by_ids=(), requires_ids=(), properties={}
        ),
    )

    used = set(re.findall(r"\$(\w+)", UNMERGE_OBLIGATION_QUERY))
    params = graph.queries[0][1]
    assert params is not None
    assert used == set(params)
