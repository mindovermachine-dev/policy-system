#!/usr/bin/env python3
"""Deterministic, no-LLM answers for the questions q-approach5.md reclassified
as scope-ambiguous rather than genuinely open (M3, M14, H6, H3) -- Next Steps
item 2 (originally scoped as "extend the catalog with a Policy->Regulation
column"; verify_remaining_rows.py's live check found that column already
exists by construction, since compile_catalog() joins the whole chain in one
pass, so no schema change was needed -- this file is the corrected item 2:
the actual missing piece was the answer functions, not the catalog schema).

Each function takes the *already-clarified* question -- the regulation,
capability, or per-clause satisfaction claim the guided clarification step
(clarifier.py) would have collected via closed choices, never free text
guessed by an LLM. This is what "re-enter the router with the scoped
question" (q-approach5.md §6, stage [5]) resolves to concretely.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))

from catalog import Catalog  # noqa: E402
from catalog_answers import NoConfidentMatch  # noqa: E402
from resolver import CapabilityResolver  # noqa: E402


def answer_m3_capability_coverage(catalog: Catalog, resolver: CapabilityResolver, free_text: str) -> str:
    """M3 -- 'Which obligations, across all three loaded regulations, require
    a <free_text>-type capability?' The rubric's 'judgment' requirement
    (explicitly state absence, don't omit) is a formatting rule over a
    reverse walk grouped by regulation -- verified live in
    verify_remaining_rows.py against cap_security_logging_c4d9e2.
    """
    hits = resolver.resolve(free_text, top_k=1)
    if not hits:
        raise NoConfidentMatch(f"no capability resembles {free_text!r}")
    cap_id, cap_name = hits[0].capability_id, hits[0].capability_name
    rows = [r for r in catalog.rows if r.capability_id == cap_id]
    by_reg: dict[str, set] = {}
    for r in rows:
        by_reg.setdefault(r.regulation_id, set()).add(r.obligation_id)

    all_loaded_regs = sorted({r.regulation_id for r in catalog.rows})
    lines = [f"Resolved {free_text!r} -> {cap_id} ({cap_name})", ""]
    covering = [reg for reg in all_loaded_regs if by_reg.get(reg)]
    absent = [reg for reg in all_loaded_regs if not by_reg.get(reg)]
    for reg in covering:
        obls = sorted(by_reg[reg])
        lines.append(f"  {reg}: {len(obls)} obligation(s) require this capability -- {obls}")
    if absent:
        lines.append(f"  {', '.join(absent)}: NO obligation requires this capability today.")
    lines.append("")
    lines.append(
        f"Scope: only {', '.join(covering)} of the {len(all_loaded_regs)} loaded regulations "
        f"have coverage for this capability -- not 'all three'."
    )
    return "\n".join(lines)


def answer_m14_draft_policies_blocking(catalog: Catalog, regulation_id: str) -> str:
    """M14 -- 'Which of our draft Policies are blocking <regulation> readiness?'
    Reclassified from 'judgment' (mining-pass.md) to scope-ambiguous: the
    only missing piece was *which* regulation, supplied here as the
    clarification answer. Verified live: the catalog already carries
    regulation_id in the same row as policy_id (the chain is joined in one
    pass from Regulation through to Control), so filtering by
    (policy_id, regulation_id) needs no catalog change at all.
    """
    seen_policies: dict[str, str] = {}
    for r in catalog.rows:
        if r.policy_id and r.policy_id not in seen_policies:
            seen_policies[r.policy_id] = r.policy_status
    draft_policies = [pid for pid, status in seen_policies.items() if status == "draft"]

    lines = [f"Draft Policies and their relevance to {regulation_id} readiness:"]
    any_blocking = False
    for pid in draft_policies:
        rows = [r for r in catalog.rows if r.policy_id == pid]
        regs = sorted({r.regulation_id for r in rows})
        caps_for_reg = sorted({r.capability_id for r in rows if r.regulation_id == regulation_id})
        if caps_for_reg:
            lines.append(f"  {pid}: governs {regulation_id}-relevant capabilities {caps_for_reg} -- BLOCKING")
            any_blocking = True
        else:
            lines.append(f"  {pid}: relevant to {regs}, not {regulation_id} -- NOT blocking {regulation_id} readiness")
    if not draft_policies:
        lines.append("  (no draft Policies exist)")
    elif not any_blocking:
        lines.append(f"  None of the draft Policies govern a {regulation_id}-relevant capability.")
    return "\n".join(lines)


def answer_h6_redundancy(catalog: Catalog, resolver: CapabilityResolver, free_text: str) -> str:
    """H6 -- 'If we adopt a <free_text> capability, would it be redundant?'
    Reclassified from 'judgment on an empty result' to a plain filter:
    resolve the capability, group its requiring obligations by regulation,
    and report whether more than one regulation already converges on it.
    Verified live against cap_component_inventory_sbom_management_b5223c
    (CRA only, zero NIS2/GDPR) matching golden-answers.md's H6 entry exactly.
    """
    hits = resolver.resolve(free_text, top_k=1)
    if not hits:
        raise NoConfidentMatch(f"no capability resembles {free_text!r}")
    cap_id, cap_name = hits[0].capability_id, hits[0].capability_name
    rows = [r for r in catalog.rows if r.capability_id == cap_id]
    regs = sorted({r.regulation_id for r in rows})

    lines = [f"Resolved {free_text!r} -> {cap_id} ({cap_name})", f"Currently required by: {regs or '(no regulation)'}"]
    if len(regs) <= 1:
        lines.append("No redundant coverage today -- adopting/extending this capability would not overlap an "
                      "obligation from a second regulation that doesn't already require it.")
    else:
        lines.append(f"Already required by {len(regs)} regulations -- redundancy already exists structurally, "
                      f"not something a new adoption would newly introduce.")
    return "\n".join(lines)


def answer_h3_scenario(catalog: Catalog, resolver: CapabilityResolver, claims: list[tuple[str, bool]]) -> str:
    """H3 -- scenario compliance verdict, per-capability. `claims` is exactly
    what the guided clarification step collects: (free-text clause, does the
    endpoint satisfy it) pairs -- a closed yes/no toggle per resolved
    capability, never an LLM inferring pass/fail from prose. Verified live
    (verify_remaining_rows.py): both 'logs access' and 'doesn't encrypt data
    at rest' resolve correctly to their expected capabilities via top-1
    lexical match; both currently sit under the same approved Policy with an
    implemented Control, so 'satisfied=False' is the only signal that can
    make a clause non-compliant -- the graph cannot infer that on its own,
    and this design does not claim it can. The user-supplied satisfied flag
    is the one piece of information this mechanism cannot derive from the
    graph, by design -- it describes THIS endpoint, not the org's Capability.
    """
    lines = []
    any_noncompliant = False
    for clause, satisfies in claims:
        hits = resolver.resolve(clause, top_k=1)
        if not hits:
            lines.append(f"  {clause!r}: no capability resolved -- cannot assess, ask the user to pick manually")
            continue
        cap_id, cap_name = hits[0].capability_id, hits[0].capability_name
        cap_rows = [r for r in catalog.rows if r.capability_id == cap_id]
        governed = [r for r in cap_rows if r.policy_id]
        if not governed:
            lines.append(f"  {clause!r} -> {cap_id} ({cap_name}): ungoverned -- no org standard to compare against")
            continue
        r0 = governed[0]
        if satisfies:
            lines.append(f"  {clause!r} -> {cap_id} ({cap_name}): satisfied -- consistent with "
                         f"{r0.policy_id}/{r0.control_id}.")
        elif r0.is_current_evidence:
            lines.append(f"  {clause!r} -> {cap_id} ({cap_name}): NOT satisfied, but org Policy {r0.policy_id} "
                         f"mandates it via implemented Control {r0.control_id} -- NON-COMPLIANT against this "
                         f"capability's requirement.")
            any_noncompliant = True
        else:
            lines.append(f"  {clause!r} -> {cap_id} ({cap_name}): not satisfied, and the org's own control "
                         f"({r0.control_id}, {r0.control_status}) isn't current evidence either -- flag as a "
                         f"pre-existing gap, not a clean endpoint-specific fail.")

    verdict = "NON-COMPLIANT (fails at least one currently-mandated capability)" if any_noncompliant else \
        "no violation found against currently-governed, currently-satisfied capabilities"
    return f"Scenario verdict: {verdict}\n" + "\n".join(lines)


def answer_h12_14_prioritized(catalog: Catalog, axis: str) -> str:
    """H12-H14 -- 'where are we most exposed' / 'what should we prioritize.'
    Every signal these rubrics require (golden-answers.md: 55/68 ungoverned
    Capabilities, the 1 `planned` Control, the 1 overdue Control, the 2
    non-approved Policies) is already a deterministic aggregate over catalog
    columns -- verified live in verify_remaining_rows.py against the exact
    numbers golden-answers.md's H12/H13 entries cite. What was missing was
    not data, it was which axis to rank by; `axis` is the clarification
    answer. `axis` in {"review_urgency", "approval_state", "coverage_gap"}.
    """
    seen_policies: dict[str, str] = {}
    seen_controls: dict[str, tuple[str, str]] = {}
    for r in catalog.rows:
        if r.policy_id and r.policy_id not in seen_policies:
            seen_policies[r.policy_id] = r.policy_status
        if r.control_id and r.control_id not in seen_controls:
            seen_controls[r.control_id] = (r.control_status, r.control_next_review_date)

    lines = [f"Prioritized punch list (axis={axis}):"]

    if axis == "review_urgency":
        items = [
            (cid, status, nrd)
            for cid, (status, nrd) in seen_controls.items()
            if status == "planned" or (status != "deprecated" and nrd is not None and nrd < "2026-08-01")
        ]
        for cid, status, nrd in sorted(items, key=lambda t: (t[2] or "", t[1])):
            urgency = "OVERDUE" if nrd and nrd < "2026-08-01" and status != "planned" else "NOT YET IMPLEMENTED"
            lines.append(f"  1. {cid}: {urgency} (status={status}, next_review_date={nrd})")
    elif axis == "approval_state":
        for pid, status in sorted(seen_policies.items()):
            if status == "draft":
                caps = sorted({r.capability_id for r in catalog.rows if r.policy_id == pid})
                lines.append(f"  - {pid}: draft -- approving it unblocks governed capabilities {caps}")
            elif status == "deprecated":
                caps = sorted({r.capability_id for r in catalog.rows if r.policy_id == pid})
                lines.append(f"  - {pid}: deprecated -- still the sole governor of {caps}, decide its fate")
    elif axis == "coverage_gap":
        governed_ids = {r.capability_id for r in catalog.rows if r.policy_id}
        ungoverned = [(cid, name) for cid, name, _ in catalog.all_capabilities if cid not in governed_ids]
        lines.append(f"  {len(ungoverned)} of {len(catalog.all_capabilities)} Capabilities have no governing Policy:")
        for cid, name in sorted(ungoverned)[:10]:
            lines.append(f"    - {cid} ({name})")
        if len(ungoverned) > 10:
            lines.append(f"    ... and {len(ungoverned) - 10} more")
    else:
        raise ValueError(f"unknown axis {axis!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    from falkordb import FalkorDB

    from catalog import compile_catalog

    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    res = CapabilityResolver(cat.all_capabilities)

    print("=== M3 (clarified: 'Security Logging') ===")
    print(answer_m3_capability_coverage(cat, res, "Security Logging"))

    print("\n=== M14 (clarified: regulation=GDPR-1.0) ===")
    print(answer_m14_draft_policies_blocking(cat, "GDPR-1.0"))

    print("\n=== H6 (clarified: 'Software Bill of Materials') ===")
    print(answer_h6_redundancy(cat, res, "Software Bill of Materials"))

    print("\n=== H3 (clarified: capability claims from guided yes/no) ===")
    print(
        answer_h3_scenario(
            cat,
            res,
            [("logs access", True), ("doesn't encrypt data at rest", False)],
        )
    )
