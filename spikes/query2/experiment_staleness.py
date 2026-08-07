#!/usr/bin/env python3
"""Live staleness test, per q-approach4.md §9 item 8: mutate a fact the
catalog depends on directly in FalkorDB, then confirm CatalogStore.get()
detects the catalog is stale and recompiles before answering, rather than
silently serving the pre-mutation row. Without this test, "staleness-checked
on read" is a claim, not a demonstrated property (§5's own words about
itself).

The mutation is applied and then reverted in a `finally` block -- this
touches live shared graph state, so the test must not leave it changed
regardless of outcome.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402

from catalog import CatalogStore  # noqa: E402

# Chosen because its full chain (policy approved, standard implemented,
# control implemented) is_current_evidence=True today -- flipping just the
# control's own status to something that's clearly not current should flip
# the trust flag too, giving an unambiguous single-field assertion instead of
# a chain where two other fields already keep it False regardless.
TARGET_CONTROL = "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual"


def main() -> None:
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    store = CatalogStore()

    before = store.get(g)
    row_before = next(r for r in before.rows if r.control_id == TARGET_CONTROL)
    print(f"[1] Initial compile #{store.compile_count}: {TARGET_CONTROL} status = {row_before.control_status!r}, "
          f"is_current_evidence = {row_before.is_current_evidence}")
    assert row_before.control_status == "implemented"
    assert row_before.is_current_evidence is True

    same = store.get(g)
    print(f"[2] Second get() with no mutation: compile #{store.compile_count} (should still be #1, no recompile)")
    assert store.compile_count == 1, "recompiled without any underlying change -- staleness check is too sensitive"
    assert same is before, "returned a different Catalog object despite unchanged signature"

    print(f"[3] Mutating {TARGET_CONTROL} live in FalkorDB: implemented -> planned "
          f"(simulating a Control slipping out of currency, e.g. a failed re-test)")
    g.query(
        "MATCH (c:Control {id: $id}) SET c.implementation_status = 'planned'",
        params={"id": TARGET_CONTROL},
    )
    try:
        after = store.get(g)
        row_after = next(r for r in after.rows if r.control_id == TARGET_CONTROL)
        print(f"[4] get() after mutation: compile #{store.compile_count}, status now = {row_after.control_status!r}, "
              f"is_current_evidence = {row_after.is_current_evidence}")
        assert store.compile_count == 2, "signature did not detect the mutation -- catalog would have served a stale row"
        assert row_after.control_status == "planned", "recompiled but still serving the old status"
        assert row_after.is_current_evidence is False, "trust flag didn't recompute off the new status"
        print("[5] PASS -- mutation detected via signature change, catalog recompiled synchronously, "
              "is_current_evidence flipped True->False off the new status, no stale row served.")
    finally:
        print(f"[6] Reverting {TARGET_CONTROL}: planned -> implemented (restoring live graph state)")
        g.query(
            "MATCH (c:Control {id: $id}) SET c.implementation_status = 'implemented'",
            params={"id": TARGET_CONTROL},
        )
        reverted = store.get(g)
        row_reverted = next(r for r in reverted.rows if r.control_id == TARGET_CONTROL)
        assert row_reverted.control_status == "implemented", "revert failed -- live graph left mutated!"
        assert row_reverted.is_current_evidence is True
        print(f"    Confirmed reverted (compile #{store.compile_count}).")


if __name__ == "__main__":
    main()
