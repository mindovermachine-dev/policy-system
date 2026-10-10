"""Fast tests for the graph mutation log's input models (issue #205, slice 3).

The models are the boundary validation: a malformed group never reaches SQL.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.graph_gateway.models import (
    GraphLogEntryDraft,
    GraphLogGroupDraft,
    GroupOutcome,
    MutationGroup,
    UpsertNode,
)


def _entry() -> GraphLogEntryDraft:
    return GraphLogEntryDraft(name="Capability", identity="cap-1", content={"title": "x"})


def test_group_with_no_entries_is_rejected_before_any_sql() -> None:
    with pytest.raises(ValidationError):
        GraphLogGroupDraft(graph="compliance", entries=())


@pytest.mark.parametrize("graph", ["", "   "])
def test_blank_graph_name_is_rejected(graph: str) -> None:
    with pytest.raises(ValidationError):
        GraphLogGroupDraft(graph=graph, entries=(_entry(),))


@pytest.mark.parametrize("field_name", ["name", "identity"])
@pytest.mark.parametrize("value", ["", "  "])
def test_blank_entry_name_or_identity_is_rejected(field_name: str, value: str) -> None:
    fields: dict[str, object] = {"name": "Capability", "identity": "cap-1", "content": {}}
    fields[field_name] = value

    with pytest.raises(ValidationError):
        GraphLogEntryDraft.model_validate(fields)


def test_entry_draft_is_immutable() -> None:
    entry = _entry()

    with pytest.raises(ValidationError):
        entry.name = "Other"  # pyright: ignore[reportAttributeAccessIssue]  # proving the model is frozen


def test_entry_draft_rejects_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        GraphLogEntryDraft.model_validate(
            {"name": "Capability", "identity": "cap-1", "content": {}, "operation": "create"}
        )


def test_a_group_does_not_request_a_checkpoint_unless_asked() -> None:
    group = MutationGroup(
        graph="g",
        audit_event_id="3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10",
        primitives=(UpsertNode(label="Capability", id="c-1"),),
    )

    assert group.checkpoint_requested is False


def test_an_outcome_defaults_to_no_checkpoint() -> None:
    outcome = GroupOutcome(graph="g", first_position=1, last_position=2, status="applied")

    assert (outcome.checkpoint, outcome.checkpoint_position) == ("not_requested", None)


@pytest.mark.parametrize(
    ("checkpoint", "position"),
    [("recorded", None), ("not_recorded", 2), ("not_requested", 2)],
)
def test_a_checkpoint_position_is_set_exactly_when_a_checkpoint_was_recorded(
    checkpoint: str, position: int | None
) -> None:
    with pytest.raises(ValidationError):
        GroupOutcome.model_validate(
            {
                "graph": "g",
                "first_position": 1,
                "last_position": 2,
                "status": "applied",
                "checkpoint": checkpoint,
                "checkpoint_position": position,
            }
        )
