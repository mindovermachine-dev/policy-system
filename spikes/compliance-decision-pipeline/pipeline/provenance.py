# © 2026 Cartman ApS. All rights reserved.
"""Resolves `source_ref` provenance chains for (C) -- the half of README's
Output-posture contract that wasn't built yet as of the last session
("(C) carries both source_ref provenance chains ... and the concrete
structured values"; PROGRESS.md's acceptance-bar pass confirmed this
concretely as the single biggest remaining gap between "mechanically
verified" and "auditor-signable").

Scope, deliberately narrow -- per SKILL.md's own Provenance rule (rule 6):
`source_ref` lives only on `EXPRESSES` (Regulation->Requirement) and
`DEFINES` (Regulation->Role) edges. Requirement resolves directly.
Obligation has no `source_ref` of its own -- its provenance is transitive,
one hop back via `SATISFIED_BY` to the Requirement(s) that satisfy it (and
from there to the Regulation edge's `source_ref`). This module resolves
exactly that one hop, not further.

Explicitly NOT resolved here: Capability and Control. Verified live this
session (see PROGRESS.md) that walking the full organizational chain back
to a Regulation for a Control (Control<-IMPLEMENTED_BY-Standard<-
SUPPORTED_BY-Policy<-GOVERNED_BY-Capability<-REQUIRES-Obligation<-
SATISFIED_BY-Requirement<-EXPRESSES-Regulation) returns dozens of
regulation-article rows for a single Control -- because the Capability a
Control implements is typically shared by many Obligations across several
Regulations. Rendering all of that as "this Control's provenance" would
reintroduce, one level up, the exact over-citation/blast-radius problem
`check_regulation_scope`'s narrowing discipline (SKILL.md rule 7) exists to
catch -- noise at best, a misleading over-claim at worst. A claim about a
Control or Capability's *existence* is an organizational-layer fact; Stage
4's existence/fanout checks already ground that directly. Only claims that
cite a specific Obligation or Requirement carry a citable regulation-text
source, and that's what this module resolves.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

from . import ps_client

_OBLIGATION_PREFIX = "obl_"
_REQUIREMENT_INFIX = "_req_"  # e.g. "CRA-1.0_req_art_13.5"


def _is_obligation_id(entity_id: str) -> bool:
    return entity_id.startswith(_OBLIGATION_PREFIX)


def _is_requirement_id(entity_id: str) -> bool:
    return _REQUIREMENT_INFIX in entity_id


def resolve_obligation_provenance(obligation_id: str) -> List[Dict[str, str]]:
    """One hop back from an Obligation to every Requirement that satisfies
    it, and that Requirement's own `EXPRESSES` edge (Regulation id +
    source_ref + requirement text). May return more than one row -- an
    Obligation can legitimately be satisfied by requirements from more than
    one Regulation (the two-layer model's whole point); may return zero
    rows for a claimed id that doesn't actually exist (the correct,
    informative result for an existence-grounding failure like CO-H2's
    fabricated 'obl_does_not_exist_deadbeef')."""
    query = (
        "MATCH (r:Regulation)-[e:EXPRESSES]->(req:Requirement)-[:SATISFIED_BY]->"
        f"(o:Obligation {{id: '{obligation_id}'}}) "
        "RETURN r.id, e.source_ref, req.id, req.text"
    )
    result = ps_client.cypher(query)
    return [
        {
            "regulation_id": row[0],
            "source_ref": row[1],
            "requirement_id": row[2],
            "requirement_text": row[3],
        }
        for row in result["rows"]
    ]


def resolve_requirement_provenance(requirement_id: str) -> List[Dict[str, str]]:
    """Direct lookup -- a Requirement's own `EXPRESSES` edge carries the
    `source_ref` (README/SKILL.md: no transitive walk needed)."""
    query = (
        "MATCH (r:Regulation)-[e:EXPRESSES]->(req:Requirement {id: '"
        + requirement_id
        + "'}) RETURN r.id, e.source_ref, req.text"
    )
    result = ps_client.cypher(query)
    return [
        {"regulation_id": row[0], "source_ref": row[1], "requirement_text": row[2]}
        for row in result["rows"]
    ]


def resolve_source_refs(entity_ids: Iterable[str]) -> Dict[str, List[Dict[str, str]]]:
    """Resolve every Obligation/Requirement id in `entity_ids` to its
    source_ref provenance chain(s). IDs of any other shape (Control,
    Capability, Policy, Standard, Role, or a bare regulation id like
    'CRA-1.0') are silently skipped -- see module docstring for why forcing
    a chain for those would be wrong, not just unbuilt. Order-preserving
    dedup isn't needed here; caller-supplied ids are already deduplicated
    upstream (compose.py collects them from a set)."""
    resolved: Dict[str, List[Dict[str, str]]] = {}
    for entity_id in entity_ids:
        if _is_obligation_id(entity_id):
            resolved[entity_id] = resolve_obligation_provenance(entity_id)
        elif _is_requirement_id(entity_id):
            resolved[entity_id] = resolve_requirement_provenance(entity_id)
    return resolved
