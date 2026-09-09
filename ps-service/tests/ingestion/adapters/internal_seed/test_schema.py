"""Tests for ps_service.ingestion.adapters.internal_seed.schema.

`_VALID_SEED_DOCUMENT` mirrors `docs/artifacts/internal-regulation-intake-format.md`'s
own worked example verbatim (one instrument, two roles, two requirements, two
obligations, two capabilities, every edge type once) so a schema-shape
regression here would also break the documented example.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.schema import validate_seed_document

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
            "properties": {
                "name": "Engineering Manager",
                "description": "Owns team execution and adherence to engineering policy.",
            },
        },
        {
            "label": "Role",
            "id": "role-security-engineer",
            "properties": {
                "name": "Security Engineer",
                "description": "Implements and verifies security controls across the SDLC.",
            },
        },
        {
            "label": "Requirement",
            "id": "req-1",
            "properties": {
                "text": (
                    "Engineering managers shall maintain approved policy governance and "
                    "controlled exception handling for all production services."
                ),
                "type": "requirement",
                "status": "active",
            },
        },
        {
            "label": "Requirement",
            "id": "req-2",
            "properties": {
                "text": (
                    "Engineering systems shall enforce strong authentication, least "
                    "privilege, and periodic access review."
                ),
                "type": "requirement",
                "status": "active",
            },
        },
        {
            "label": "Obligation",
            "id": "obl-policy-governance",
            "properties": {
                "text": "Maintain approved policy governance and controlled exception handling"
            },
        },
        {
            "label": "Obligation",
            "id": "obl-access-control",
            "properties": {
                "text": "Enforce strong authentication, least privilege, and periodic access review"
            },
        },
        {
            "label": "Capability",
            "id": "cap-policy-exception-governance",
            "properties": {"name": "Policy Exception Governance"},
        },
        {
            "label": "Capability",
            "id": "cap-access-control",
            "properties": {"name": "Access Control & Authentication"},
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
            "type": "DEFINES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Role", "id": "role-security-engineer"},
            "properties": {"source_ref": "Sec. 1"},
        },
        {
            "type": "EXPRESSES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Requirement", "id": "req-1"},
            "properties": {"source_ref": "4.1"},
        },
        {
            "type": "EXPRESSES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Requirement", "id": "req-2"},
            "properties": {"source_ref": "4.3"},
        },
        {
            "type": "HAS",
            "from": {"label": "Role", "id": "role-eng-manager"},
            "to": {"label": "Obligation", "id": "obl-policy-governance"},
        },
        {
            "type": "HAS",
            "from": {"label": "Role", "id": "role-security-engineer"},
            "to": {"label": "Obligation", "id": "obl-access-control"},
        },
        {
            "type": "SATISFIED_BY",
            "from": {"label": "Requirement", "id": "req-1"},
            "to": {"label": "Obligation", "id": "obl-policy-governance"},
        },
        {
            "type": "SATISFIED_BY",
            "from": {"label": "Requirement", "id": "req-2"},
            "to": {"label": "Obligation", "id": "obl-access-control"},
        },
        {
            "type": "REQUIRES",
            "from": {"label": "Obligation", "id": "obl-policy-governance"},
            "to": {"label": "Capability", "id": "cap-policy-exception-governance"},
        },
        {
            "type": "REQUIRES",
            "from": {"label": "Obligation", "id": "obl-access-control"},
            "to": {"label": "Capability", "id": "cap-access-control"},
        },
    ],
}


def test_validate_seed_document_accepts_the_documented_worked_example() -> None:
    """The intake-format doc's own worked example is schema-valid, unchanged."""
    validate_seed_document(copy.deepcopy(_VALID_SEED_DOCUMENT))


def test_validate_seed_document_rejects_unknown_top_level_field() -> None:
    """A submitted `graph_name` (or any other unrecognized top-level field) is rejected (D2).

    AC-BI-010's "graph_name field can never redirect a write" guarantee holds
    structurally here: `additionalProperties: false` at the schema's top level
    means the field is rejected outright, not silently ignored.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["graph_name"] = "policy_system"

    with pytest.raises(InternalSeedError) as excinfo:
        validate_seed_document(document)

    assert "graph_name" in str(excinfo.value) or "additional" in str(excinfo.value).lower()


def test_validate_seed_document_rejects_missing_required_node_property() -> None:
    """A `RegulatoryInstrument` missing a required property (e.g. `source_type`) is rejected."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    del document["nodes"][0]["properties"]["source_type"]

    with pytest.raises(InternalSeedError) as excinfo:
        validate_seed_document(document)

    assert "source_type" in str(excinfo.value)


def test_validate_seed_document_rejects_unknown_node_label() -> None:
    """A node labeled outside the allow-list (e.g. `PracticeArea`) is rejected (D2/AC-BI-002).

    `PracticeArea`/`RiskPath` remain genuinely out of scope for this format
    even after GH #76 Slice 3 -- `Policy` (Slice 1), `Standard` (Slice 2),
    and `Control` (Slice 3) are all accepted now, so `Control` itself is no
    longer a valid "still rejected" example.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"].append(
        {"label": "PracticeArea", "id": "pa-1", "properties": {"name": "Not allowed"}}
    )

    with pytest.raises(InternalSeedError):
        validate_seed_document(document)


def test_validate_seed_document_accepts_a_policy_node_and_governed_by_edge() -> None:
    """GH #76 AC-BI-001 (Policy portion): a `Policy` node + `GOVERNED_BY` edge
    from a Capability is schema-valid -- red before the schema gained
    `Policy`/`GOVERNED_BY` (issue #76 Slice 1), green after.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"].append(
        {
            "label": "Policy",
            "id": "pol-1",
            "properties": {"title": "Access Control Policy", "status": "draft"},
        }
    )
    document["edges"].append(
        {
            "type": "GOVERNED_BY",
            "from": {"label": "Capability", "id": "cap-access-control"},
            "to": {"label": "Policy", "id": "pol-1"},
        }
    )

    validate_seed_document(document)


def test_validate_seed_document_accepts_a_standard_node_and_supported_by_edge() -> None:
    """GH #76 AC-BI-001 (Standard portion): a `Standard` node + `SUPPORTED_BY` edge
    from a Policy is schema-valid -- red before the schema gained `Standard`/
    `SUPPORTED_BY` (issue #76 Slice 2), green after.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"].append(
        {
            "label": "Policy",
            "id": "pol-1",
            "properties": {"title": "Access Control Policy", "status": "draft"},
        }
    )
    document["nodes"].append(
        {
            "label": "Standard",
            "id": "std-1",
            "properties": {
                "title": "Access Control Standard",
                "implementation_status": "draft",
            },
        }
    )
    document["edges"].append(
        {
            "type": "GOVERNED_BY",
            "from": {"label": "Capability", "id": "cap-access-control"},
            "to": {"label": "Policy", "id": "pol-1"},
        }
    )
    document["edges"].append(
        {
            "type": "SUPPORTED_BY",
            "from": {"label": "Policy", "id": "pol-1"},
            "to": {"label": "Standard", "id": "std-1"},
        }
    )

    validate_seed_document(document)


def test_validate_seed_document_accepts_a_control_node_and_implemented_by_edge() -> None:
    """GH #76 AC-BI-001 (Control portion): a `Control` node + `IMPLEMENTED_BY` edge
    from a Standard is schema-valid -- red before the schema gained `Control`/
    `IMPLEMENTED_BY` (issue #76 Slice 3), green after. AC-BI-001 is now fully
    satisfied: all three governance labels and all three governance edge types
    are schema-validated.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"].append(
        {
            "label": "Policy",
            "id": "pol-1",
            "properties": {"title": "Access Control Policy", "status": "draft"},
        }
    )
    document["nodes"].append(
        {
            "label": "Standard",
            "id": "std-1",
            "properties": {
                "title": "Access Control Standard",
                "implementation_status": "draft",
            },
        }
    )
    document["nodes"].append(
        {
            "label": "Control",
            "id": "ctrl-1",
            "properties": {
                "type": "automated",
                "title": "Automated Access Review Check",
                "implementation_status": "planned",
            },
        }
    )
    document["edges"].append(
        {
            "type": "GOVERNED_BY",
            "from": {"label": "Capability", "id": "cap-access-control"},
            "to": {"label": "Policy", "id": "pol-1"},
        }
    )
    document["edges"].append(
        {
            "type": "SUPPORTED_BY",
            "from": {"label": "Policy", "id": "pol-1"},
            "to": {"label": "Standard", "id": "std-1"},
        }
    )
    document["edges"].append(
        {
            "type": "IMPLEMENTED_BY",
            "from": {"label": "Standard", "id": "std-1"},
            "to": {"label": "Control", "id": "ctrl-1"},
        }
    )

    validate_seed_document(document)


def test_validate_seed_document_rejects_policy_with_confidence_property() -> None:
    """Design Decision 2 (PLAN.md §3): `confidence` is never accepted on
    `Policy` at the intake boundary -- `additionalProperties: false` makes
    submitting one a schema violation, not a silently-dropped field.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"].append(
        {
            "label": "Policy",
            "id": "pol-1",
            "properties": {
                "title": "Access Control Policy",
                "status": "draft",
                "confidence": 0.9,
            },
        }
    )

    with pytest.raises(InternalSeedError):
        validate_seed_document(document)


def test_validate_seed_document_rejects_source_type_other_than_internal() -> None:
    """A `RegulatoryInstrument.source_type` other than `"internal"` is rejected (AC-BI-003)."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"][0]["properties"]["source_type"] = "external"

    with pytest.raises(InternalSeedError):
        validate_seed_document(document)
