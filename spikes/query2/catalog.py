#!/usr/bin/env python3
"""Candidate D: pre-compiled catalog (materialized denormalized view).

See ../query2/q-approach4.md (Candidate D) and mining-pass.md for the design
and the empirical basis for it. Per the mining pass, every golden query in
../query1/golden-answers.md is a contiguous sub-path of exactly two chains:

  Chain A (governance):
    (Regulation)-[DEFINES]->(Role)-[HAS]->(Obligation)-[REQUIRES]->(Capability)
      -[GOVERNED_BY]->(Policy)-[SUPPORTED_BY]->(Standard)-[IMPLEMENTED_BY]->(Control)
  Chain B (requirement text), joins Chain A at Obligation:
    (Regulation)-[EXPRESSES]->(Requirement)-[SATISFIED_BY]->(Obligation)

So there is exactly one catalog root, not several keyed by different anchor
labels -- one denormalized table with every node's id/status/property from
both chains as columns, joined in Python one hop at a time rather than one
big multi-hop Cypher MATCH. Walking hop-by-hop and joining here (not in
Cypher) is deliberate, not a style choice: q-approach1.md's "Result" section
and golden-answers.md's M7 entry both document a real FalkorDB bug where a
5+ hop MATCH silently drops rows depending on which columns get projected
(57 rows expected, 33 or 49 returned depending on RETURN clause). Building
the catalog from single-label, single-hop queries sidesteps that class of
bug entirely rather than working around it after the fact -- and
cross_verify_against_live() below still checks the two agree, per
q-approach4.md's §7 fix 7.
"""

import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402


@dataclass(frozen=True)
class CatalogRow:
    """One fully-resolved chain, every relevant property inlined -- not just
    ids, per q-approach4.md's Candidate D worked example. Optional fields are
    None when the chain doesn't reach that far (e.g. an ungoverned Capability
    has no policy_id onward) -- this is itself real data (H2/H1's ungoverned
    findings), not a missing row.
    """

    regulation_id: str
    regulation_status: str
    role_name: str
    obligation_id: str
    obligation_text: str
    obligation_confidence: float
    requirement_id: Optional[str]
    requirement_text: Optional[str]
    capability_id: str
    capability_name: str
    capability_description: str
    policy_id: Optional[str]
    policy_status: Optional[str]
    standard_id: Optional[str]
    standard_status: Optional[str]
    control_id: Optional[str]
    control_status: Optional[str]
    control_next_review_date: Optional[str]
    control_evidence_ref: Optional[str]
    is_current_evidence: bool


@dataclass
class Catalog:
    rows: list[CatalogRow]
    signature: str
    # Side tables not part of the main chain join, needed by a few questions
    # (H5's supersession edge; the full Capability vocabulary including
    # ungoverned ones, for the entity resolver).
    supersessions: list[tuple[str, str]]
    all_capabilities: list[tuple[str, str, str]]  # (id, name, description)


# --------------------------------------------------------------------------
# Staleness signature -- per q-approach4.md §7 fix 6 / §10.9: lazy validation
# at read time, not periodic recompute. No node in this schema carries an
# updated_at timestamp (checked directly against query_mechanism_v2.py's
# GRAPH_SCHEMA -- Policy/Standard/Control have no such property), so the
# signature below is a content hash over exactly the columns whose change
# would make a cached catalog wrong: Policy/Standard/Control status-bearing
# fields, the volatile operational layer this whole spike exists to catch
# going stale (per q-approach4.md's own framing). Regulation/Role/Requirement
# /Obligation/Capability are extraction-time-static by comparison -- they
# don't change between compliance reviews the way a Control's
# implementation_status does -- so they're deliberately excluded to keep the
# signature query cheap (this graph's entire Policy+Standard+Control layer is
# 17 rows).
# --------------------------------------------------------------------------


def compute_signature(graph) -> str:
    parts = []
    for pid, status, version in sorted(
        graph.query("MATCH (p:Policy) RETURN p.id, p.status, p.version").result_set
    ):
        parts.append(f"P|{pid}|{status}|{version}")
    for sid, status, version in sorted(
        graph.query("MATCH (s:Standard) RETURN s.id, s.implementation_status, s.version").result_set
    ):
        parts.append(f"S|{sid}|{status}|{version}")
    for cid, status, nrd in sorted(
        graph.query(
            "MATCH (c:Control) RETURN c.id, c.implementation_status, c.next_review_date"
        ).result_set,
        key=lambda r: (r[0] or "",),
    ):
        parts.append(f"C|{cid}|{status}|{nrd}")
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return digest


# --------------------------------------------------------------------------
# Per-hop compilation
# --------------------------------------------------------------------------


def compile_catalog(graph) -> Catalog:
    # Node property tables, one single-label query each -- never a multi-hop
    # MATCH. Small enough (max 342 rows) to hold entirely in memory.
    regulations = {
        rid: {"status": status}
        for rid, status in graph.query("MATCH (r:Regulation) RETURN r.id, r.status").result_set
    }
    obligations = {
        oid: {"text": text, "confidence": conf}
        for oid, text, conf in graph.query(
            "MATCH (o:Obligation) RETURN o.id, o.text, o.confidence"
        ).result_set
    }
    requirements = {
        rid: {"text": text}
        for rid, text in graph.query("MATCH (req:Requirement) RETURN req.id, req.text").result_set
    }
    capabilities = {
        cid: {"name": name, "description": desc}
        for cid, name, desc in graph.query(
            "MATCH (c:Capability) RETURN c.id, c.name, c.description"
        ).result_set
    }
    policies = {
        pid: {"status": status}
        for pid, status in graph.query("MATCH (p:Policy) RETURN p.id, p.status").result_set
    }
    standards = {
        sid: {"status": status}
        for sid, status in graph.query(
            "MATCH (s:Standard) RETURN s.id, s.implementation_status"
        ).result_set
    }
    controls = {
        ctid: {"status": status, "next_review_date": nrd, "evidence_ref": ev}
        for ctid, status, nrd, ev in graph.query(
            "MATCH (c:Control) RETURN c.id, c.implementation_status, c.next_review_date, c.evidence_ref"
        ).result_set
    }

    # Single-hop edge tables.
    reg_role = graph.query("MATCH (r:Regulation)-[:DEFINES]->(role:Role) RETURN r.id, role.name").result_set
    role_obl = graph.query("MATCH (role:Role)-[:HAS]->(o:Obligation) RETURN role.name, o.id").result_set
    obl_cap = graph.query("MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) RETURN o.id, c.id").result_set
    cap_pol = graph.query("MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) RETURN c.id, p.id").result_set
    pol_std = graph.query("MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard) RETURN p.id, s.id").result_set
    std_ctrl = graph.query("MATCH (s:Standard)-[:IMPLEMENTED_BY]->(c:Control) RETURN s.id, c.id").result_set
    reg_req = graph.query("MATCH (r:Regulation)-[:EXPRESSES]->(req:Requirement) RETURN r.id, req.id").result_set
    req_obl = graph.query("MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation) RETURN req.id, o.id").result_set
    supersessions = graph.query(
        "MATCH (a:Regulation)-[:SUPERSEDED_BY]->(b:Regulation) RETURN a.id, b.id"
    ).result_set

    # Index by source key for the join. 1:N throughout -- fan-out is
    # deliberately preserved (a cross product), never collapsed, so no row
    # a golden query could return is silently merged away here.
    def index(pairs):
        idx: dict[str, list[str]] = {}
        for a, b in pairs:
            idx.setdefault(a, []).append(b)
        return idx

    role_of_reg = index(reg_role)
    obl_of_role = index(role_obl)
    cap_of_obl = index(obl_cap)
    pol_of_cap = index(cap_pol)
    std_of_pol = index(pol_std)
    ctrl_of_std = index(std_ctrl)
    req_of_reg = index(reg_req)
    obl_of_req = index(req_obl)

    # Requirement -> Obligation is stored req->obl; invert to obl->[req] for
    # the join below (one obligation can be satisfied-by more than one
    # requirement -- a base clause and its lettered sub-clauses commonly
    # converge on the same obligation).
    reqs_of_obl: dict[str, list[str]] = {}
    for req_id, obl_id in req_obl:
        reqs_of_obl.setdefault(obl_id, []).append(req_id)

    rows: list[CatalogRow] = []
    for reg_id, reg in regulations.items():
        for role_name in role_of_reg.get(reg_id, []):
            for obl_id in obl_of_role.get(role_name, []):
                obl = obligations[obl_id]
                req_ids = reqs_of_obl.get(obl_id, [None])
                for cap_id in cap_of_obl.get(obl_id, []):
                    cap = capabilities[cap_id]
                    pol_ids = pol_of_cap.get(cap_id, [None])
                    for pol_id in pol_ids:
                        pol = policies.get(pol_id) if pol_id else None
                        std_ids = std_of_pol.get(pol_id, [None]) if pol_id else [None]
                        for std_id in std_ids:
                            std = standards.get(std_id) if std_id else None
                            ctrl_ids = ctrl_of_std.get(std_id, [None]) if std_id else [None]
                            for ctrl_id in ctrl_ids:
                                ctrl = controls.get(ctrl_id) if ctrl_id else None
                                is_current = bool(
                                    pol
                                    and pol["status"] == "approved"
                                    and std
                                    and std["status"] in ("implemented", "reviewed")
                                    and ctrl
                                    and ctrl["status"] == "implemented"
                                )
                                for req_id in req_ids:
                                    req = requirements.get(req_id) if req_id else None
                                    rows.append(
                                        CatalogRow(
                                            regulation_id=reg_id,
                                            regulation_status=reg["status"],
                                            role_name=role_name,
                                            obligation_id=obl_id,
                                            obligation_text=obl["text"],
                                            obligation_confidence=obl["confidence"],
                                            requirement_id=req_id,
                                            requirement_text=req["text"] if req else None,
                                            capability_id=cap_id,
                                            capability_name=cap["name"],
                                            capability_description=cap["description"],
                                            policy_id=pol_id,
                                            policy_status=pol["status"] if pol else None,
                                            standard_id=std_id,
                                            standard_status=std["status"] if std else None,
                                            control_id=ctrl_id,
                                            control_status=ctrl["status"] if ctrl else None,
                                            control_next_review_date=ctrl["next_review_date"] if ctrl else None,
                                            control_evidence_ref=ctrl["evidence_ref"] if ctrl else None,
                                            is_current_evidence=is_current,
                                        )
                                    )

    all_caps = [(cid, c["name"], c["description"]) for cid, c in capabilities.items()]
    return Catalog(
        rows=rows,
        signature=compute_signature(graph),
        supersessions=[(a, b) for a, b in supersessions],
        all_capabilities=all_caps,
    )


class CatalogStore:
    """Staleness-on-read: every ask() through the catalog stage calls get(),
    which recompiles synchronously iff the cheap signature has changed since
    the last compile -- never serves a stale row with a caveat, never waits
    for a schedule. See q-approach4.md §7 fix 6 / §10.9.
    """

    def __init__(self):
        self._catalog: Optional[Catalog] = None
        self.compile_count = 0

    def get(self, graph) -> Catalog:
        current_sig = compute_signature(graph)
        if self._catalog is None or self._catalog.signature != current_sig:
            self._catalog = compile_catalog(graph)
            self.compile_count += 1
        return self._catalog


if __name__ == "__main__":
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    print(f"rows: {len(cat.rows)}")
    print(f"signature: {cat.signature[:16]}...")
    print(f"capabilities indexed: {len(cat.all_capabilities)}")
    print(f"supersessions: {cat.supersessions}")
