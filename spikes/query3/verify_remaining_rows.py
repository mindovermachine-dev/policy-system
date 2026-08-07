#!/usr/bin/env python3
"""q-approach5.md Next Steps item 1: independently verify, live against
FalkorDB, the rows of q-approach5.md's §4 table that weren't yet checked
(M14 and H6 already were, inline in q-approach5.md itself). This extends the
same "measure, don't assume" check to M3, H3, H12-H14's axis claim, and H8's
top-k shape.

No LLM calls -- this is pure graph/resolver verification, same as catalog.py
and resolver.py's own __main__ blocks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402

from catalog import compile_catalog  # noqa: E402
from resolver import CapabilityResolver  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> None:
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    resolver = CapabilityResolver(cat.all_capabilities)

    # -- M3 -----------------------------------------------------------------
    section("M3: 'Security Logging'-type capability across all 3 regulations")
    cap_id = "cap_security_logging_c4d9e2"
    rows = [r for r in cat.rows if r.capability_id == cap_id]
    regs = sorted({r.regulation_id for r in rows})
    print(f"{cap_id} required by obligations from regulations: {regs}")
    print(f"NIS2-1.0 present: {'NIS2-1.0' in regs}")
    print(f"GDPR-1.0 present: {'GDPR-1.0' in regs}")
    ok_m3 = regs == ["CRA-1.0", "HELVEX-SOP-1.0"]
    print(f"Matches golden-answers.md's stated scope (CRA + Helvex only, NOT NIS2/GDPR): {ok_m3}")

    # -- H3 -------------------------------------------------------------------
    section("H3: scenario -> 2 capabilities, resolvable via split clauses?")
    clauses = {
        "logs access": None,
        "doesn't encrypt data at rest": None,
    }
    for clause in clauses:
        hits = resolver.resolve(clause, top_k=3)
        print(f"  {clause!r} -> {[(h.capability_id, round(h.score, 2)) for h in hits]}")

    expected = {
        "logs access": "cap_access_control_authentication_151816",
        "doesn't encrypt data at rest": "cap_data_encryption_0e50d3",
    }
    for clause, expected_id in expected.items():
        hits = resolver.resolve(clause, top_k=3)
        top = hits[0].capability_id if hits else None
        print(f"  clause={clause!r}: top hit={top!r}, expected={expected_id!r}, match={top == expected_id}")

    print("\n  Governance status per resolved capability (what a per-capability verdict needs):")
    for cap_id2 in expected.values():
        cap_rows = [r for r in cat.rows if r.capability_id == cap_id2]
        governed = [r for r in cap_rows if r.policy_id]
        if governed:
            r0 = governed[0]
            print(
                f"    {cap_id2}: policy={r0.policy_id} ({r0.policy_status}), "
                f"control={r0.control_id} ({r0.control_status}), current_evidence={r0.is_current_evidence}"
            )
        else:
            print(f"    {cap_id2}: ungoverned")

    # -- H12/H13/H14 axis claim -----------------------------------------------
    section("H12-H14: are the cited signals real, sortable, deterministic columns?")

    total_caps = len(cat.all_capabilities)
    governed_cap_ids = {r.capability_id for r in cat.rows if r.policy_id}
    ungoverned = total_caps - len(governed_cap_ids)
    print(f"  Capabilities: {total_caps} total, {len(governed_cap_ids)} governed, {ungoverned} ungoverned")
    print(f"  Matches golden-answers.md H12/H13 (68 total, 13 governed, 55 ungoverned): "
          f"{total_caps == 68 and len(governed_cap_ids) == 13 and ungoverned == 55}")

    seen_policies = {}
    for r in cat.rows:
        if r.policy_id and r.policy_id not in seen_policies:
            seen_policies[r.policy_id] = r.policy_status
    from collections import Counter

    pol_counts = Counter(seen_policies.values())
    print(f"  Policies: {len(seen_policies)} total, by status: {dict(pol_counts)}")

    seen_controls = {}
    for r in cat.rows:
        if r.control_id and r.control_id not in seen_controls:
            seen_controls[r.control_id] = (r.control_status, r.control_next_review_date)
    ctrl_counts = Counter(v[0] for v in seen_controls.values())
    print(f"  Controls: {len(seen_controls)} total, by status: {dict(ctrl_counts)}")

    overdue = [
        (cid, nrd)
        for cid, (status, nrd) in seen_controls.items()
        if status != "deprecated" and nrd is not None and nrd < "2026-08-01"
    ]
    print(f"  Overdue controls (next_review_date < 2026-08-01, excl. deprecated): {overdue}")

    print(
        "\n  Real sortable axes available for a prioritization pick: "
        "Control.next_review_date (deadline), Control.implementation_status / "
        "Standard.implementation_status / Policy.status (approval-state), "
        "Capability governed-vs-ungoverned (coverage). No 'business criticality' "
        "column exists anywhere in the schema -- an axis picker must only offer "
        "axes that are real graph columns, per q-approach5.md §5's own warning."
    )

    # Deterministic H14-shape punch list, sorted by review-date urgency then status severity
    section("Deterministic H14-style punch list once an axis (review urgency) is picked")
    planned_or_overdue = [
        (cid, status, nrd)
        for cid, (status, nrd) in seen_controls.items()
        if status == "planned" or (status != "deprecated" and nrd is not None and nrd < "2026-08-01")
    ]
    for cid, status, nrd in sorted(planned_or_overdue, key=lambda t: (t[2] or "", t[1])):
        print(f"  - {cid}: status={status}, next_review_date={nrd}")
    draft_policies = [pid for pid, status in seen_policies.items() if status == "draft"]
    deprecated_policies = [pid for pid, status in seen_policies.items() if status == "deprecated"]
    for pid in draft_policies:
        print(f"  - {pid}: draft -- move to approved to unblock its governed capabilities")
    for pid in deprecated_policies:
        print(f"  - {pid}: deprecated -- decide its fate, still sole governor of some capabilities")

    # -- H8 top-k shape ---------------------------------------------------------
    section("H8: does any single free-text call reach the 5 golden capabilities?")
    golden_h8 = {
        "cap_data_encryption_0e50d3",
        "cap_access_control_authentication_151816",
        "cap_security_logging_c4d9e2",
        "cap_data_protection_impact_assessment_a51acb",
        "cap_secure_data_removal_portability_3d7885",
    }
    queries = [
        "stores customer PII",
        "PII",
        "personal data storage compliance",
        "customer personal data database",
    ]
    for q in queries:
        hits = resolver.resolve(q, top_k=10)
        hit_ids = {h.capability_id for h in hits}
        overlap = hit_ids & golden_h8
        print(f"  {q!r} -> {len(hits)} hits, overlap with golden 5: {sorted(overlap)} ({len(overlap)}/5)")


if __name__ == "__main__":
    main()
