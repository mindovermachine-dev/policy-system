"""Fast tests for the graph mutation log's input models (issue #205, slice 3).

The models are the boundary validation: a malformed group never reaches SQL.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ps_service.graph_gateway.models import GraphLogEntryDraft, GraphLogGroupDraft


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
