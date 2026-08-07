#!/usr/bin/env python3
"""Independent per-hop cross-verification of the compiled catalog, per
q-approach4.md's §7 fix 7 / §9 item 7: the catalog's deepest rows are the
exact chain shape (5+ hops) that already hid a real silent bug in
query_mechanism_v1.py's own history (the FalkorDB column-projection issue
documented in q-approach1.md's "Result" section and golden-answers.md's M7
entry -- a 6-hop MATCH returning 33 or 49 rows instead of 57 depending on
which columns were projected). This is the specific check that caught that
bug once, run against the new catalog rather than assumed safe because it's
"just a join.

Two independent checks:
1. Reproduce golden-answers.md's own M7 number (57 GDPR chains) by filtering
   the catalog to requirement_id STARTS WITH "GDPR" and a non-null control_id.
2. Run the same single 6-hop live Cypher query M7/q-approach1.md used
   (the one the bug was originally found in) and diff its rows against the
   catalog's, row-for-row on ids -- not just comparing counts, since two
   different wrong counts could coincidentally differ from each other but
   both be wrong.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402

from catalog import compile_catalog  # noqa: E402


def main() -> None:
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)

    # Check 1: M7's golden count (57 GDPR chains reaching a Control).
    gdpr_full_chains = [
        r for r in cat.rows if r.requirement_id and r.requirement_id.startswith("GDPR") and r.control_id
    ]
    print(f"[Check 1] GDPR requirement->...->Control chains in catalog: {len(gdpr_full_chains)}")
    print(f"          golden-answers.md M7 says: 57")
    assert len(gdpr_full_chains) == 57, f"MISMATCH: catalog gives {len(gdpr_full_chains)}, golden says 57"
    print("          MATCH\n")

    current = sum(1 for r in gdpr_full_chains if r.is_current_evidence)
    stale = len(gdpr_full_chains) - current
    print(f"[Check 1b] is_current_evidence split: {current} current / {stale} stale")
    print(f"           golden-answers.md M7 says: 31 current / 26 stale")
    assert (current, stale) == (31, 26), f"MISMATCH: catalog gives {current}/{stale}, golden says 31/26"
    print("           MATCH\n")

    # Check 2: independently re-run the exact single 6-hop query M7 uses
    # live, and diff row-for-row (by the 6 matched node ids) against the
    # catalog's equivalent slice -- not just a count comparison.
    live = g.query(
        "MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)"
        "-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control) "
        'WHERE req.id STARTS WITH "GDPR" '
        "RETURN req.id, o.id, c.id, p.id, s.id, ctrl.id"
    ).result_set
    live_keys = {tuple(row) for row in live}
    catalog_keys = {
        (r.requirement_id, r.obligation_id, r.capability_id, r.policy_id, r.standard_id, r.control_id)
        for r in gdpr_full_chains
    }
    print(f"[Check 2] live 6-hop MATCH row count: {len(live)} (raw, before dedup: check for the known bug)")
    only_in_live = live_keys - catalog_keys
    only_in_catalog = catalog_keys - live_keys
    print(f"          rows only in live query: {len(only_in_live)}")
    print(f"          rows only in catalog:    {len(only_in_catalog)}")
    if only_in_live:
        print(f"          sample only-in-live: {list(only_in_live)[:3]}")
    if only_in_catalog:
        print(f"          sample only-in-catalog: {list(only_in_catalog)[:3]}")
    assert live_keys == catalog_keys, "MISMATCH: live 6-hop query and per-hop-joined catalog disagree row-for-row"
    print("          MATCH -- per-hop join agrees with direct 6-hop query, byte-for-byte on ids\n")

    # Check 3: H2's ungoverned-capability count (55 of 68), a *negative*
    # check -- capabilities that should have NO policy_id in the catalog at all.
    governed_cap_ids = {r.capability_id for r in cat.rows if r.policy_id}
    ungoverned = [cid for cid, _, _ in cat.all_capabilities if cid not in governed_cap_ids]
    print(f"[Check 3] ungoverned capabilities (catalog): {len(ungoverned)} of {len(cat.all_capabilities)}")
    print(f"          golden-answers.md H2 says: 55 of 68")
    assert len(ungoverned) == 55, f"MISMATCH: catalog gives {len(ungoverned)}, golden says 55"
    print("          MATCH\n")

    print("All cross-verification checks passed.")


if __name__ == "__main__":
    main()
