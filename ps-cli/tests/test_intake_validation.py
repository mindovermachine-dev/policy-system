"""Tests for ps_cli.intake_validation: validate_local_seed_file (D3/D7, PLAN.md §6 S1).

`_VALID_SEED_DOCUMENT` mirrors `docs/artifacts/internal-regulation-intake-format.md`'s
own worked example verbatim, kept in lockstep with the equivalent fixture in
`ps-service/tests/ingestion/adapters/internal_seed/test_schema.py` (both members
validate the same document against their own packaged copy of the same schema, D7).
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from ps_cli.errors import PsCliError
from ps_cli.intake_validation import validate_local_seed_file

if TYPE_CHECKING:
    from pathlib import Path

_VALID_SEED_DOCUMENT: dict[str, Any] = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "ENGPRAC-3.0",
            "properties": {
                "title": "Engineering Practices Policy",
                "source_type": "internal",
                "effective_date": "2026-08-01",
                "version": "3.0",
                "status": "active",
            },
        },
        {
            "label": "Role",
            "id": "role-eng-manager",
            "properties": {"name": "Engineering Manager"},
        },
        {
            "label": "Requirement",
            "id": "req-1",
            "properties": {
                "text": "Engineering managers shall maintain approved policy governance.",
                "type": "requirement",
            },
        },
        {
            "label": "Obligation",
            "id": "obl-policy-governance",
            "properties": {"text": "Maintain approved policy governance"},
        },
        {
            "label": "Capability",
            "id": "cap-policy-exception-governance",
            "properties": {"name": "Policy Exception Governance"},
        },
    ],
    "edges": [
        {
            "type": "DEFINES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Role", "id": "role-eng-manager"},
            "properties": {"source_ref": "Sec. 1"},
        },
        {
            "type": "EXPRESSES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Requirement", "id": "req-1"},
            "properties": {"source_ref": "4.1"},
        },
        {
            "type": "HAS",
            "from": {"label": "Role", "id": "role-eng-manager"},
            "to": {"label": "Obligation", "id": "obl-policy-governance"},
        },
        {
            "type": "SATISFIED_BY",
            "from": {"label": "Requirement", "id": "req-1"},
            "to": {"label": "Obligation", "id": "obl-policy-governance"},
        },
        {
            "type": "REQUIRES",
            "from": {"label": "Obligation", "id": "obl-policy-governance"},
            "to": {"label": "Capability", "id": "cap-policy-exception-governance"},
        },
    ],
}


def _write_seed_file(tmp_path: Path, document: dict[str, Any]) -> Path:
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(document), encoding="utf-8")
    return seed_path


def test_validate_local_seed_file_accepts_the_documented_worked_example(
    tmp_path: Path,
) -> None:
    """A schema-conformant file is accepted -- no exception raised."""
    seed_path = _write_seed_file(tmp_path, copy.deepcopy(_VALID_SEED_DOCUMENT))

    validate_local_seed_file(seed_path)


def test_rejects_seed_missing_required_property(tmp_path: Path) -> None:
    """A `RegulatoryInstrument` missing a required property (`source_type`) is rejected.

    The raised `PsCliError` names the specific missing property -- AC-BI-019's
    "naming the specific violation, not a generic parse error" -- not just
    "invalid JSON" or "schema violation".
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    del document["nodes"][0]["properties"]["source_type"]
    seed_path = _write_seed_file(tmp_path, document)

    with pytest.raises(PsCliError) as excinfo:
        validate_local_seed_file(seed_path)

    assert "source_type" in excinfo.value.msg


def test_validate_local_seed_file_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    """A submitted `graph_name` field is rejected outright (D2), not silently ignored."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["graph_name"] = "policy_system"
    seed_path = _write_seed_file(tmp_path, document)

    with pytest.raises(PsCliError):
        validate_local_seed_file(seed_path)


def test_validate_local_seed_file_raises_when_file_missing(tmp_path: Path) -> None:
    """A nonexistent fixture path raises `PsCliError`, not an unhandled `FileNotFoundError`."""
    missing_path = tmp_path / "does-not-exist.json"

    with pytest.raises(PsCliError) as excinfo:
        validate_local_seed_file(missing_path)

    assert "not found" in excinfo.value.msg


def test_validate_local_seed_file_raises_on_malformed_json(tmp_path: Path) -> None:
    """A file that is not valid JSON raises `PsCliError` naming the parse failure."""
    seed_path = tmp_path / "seed.json"
    seed_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(PsCliError) as excinfo:
        validate_local_seed_file(seed_path)

    assert "not valid JSON" in excinfo.value.msg
