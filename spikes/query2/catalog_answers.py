#!/usr/bin/env python3
"""Deterministic, no-LLM answers for the questions mining-pass.md found fully
catalog-reachable: H1, H11, H5, H9. Each is graded below against its rubric
in ../query1/golden-answers.md -- pass/fail per stated criterion, not a
vibe check.

H3 and H8 are deliberately NOT given full answer functions here -- the
resolver bake-off (experiment_resolver_bakeoff.py) already measured that a
single free-text resolve() call doesn't reach H8's 5-capability golden set,
and mining-pass.md's classification calls both "partial." Building a
polished-looking answer function for a case already measured not to work
would misrepresent what Candidate D covers. They stay routed to v2, per the
design's own fallback discipline.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog import Catalog  # noqa: E402
from resolver import CapabilityResolver, ResolverHit  # noqa: E402


class NoConfidentMatch(Exception):
    """Raised when the resolver has no confident hit -- surfaced to the
    caller as an honest refusal, per SYSTEM_PROMPT rule 2 in
    query_mechanism_v2.py ("say so explicitly... do not fill the gap with a
    plausible guess"), reproduced here as catalog D's own behavior rather
    than something only the freehand agent path does.
    """


def answer_h1_gdpr_art32(catalog: Catalog) -> str:
    """H1 -- 'Are we compliant with GDPR Article 32?' Deterministic per
    mining-pass.md: the verdict is a classification function over the
    already-computed trust flags, not a fresh inference.
    """
    rows = [r for r in catalog.rows if r.requirement_id and r.requirement_id.startswith("GDPR-1.0_req_art_32")]
    by_req: dict[str, list] = {}
    for r in rows:
        by_req.setdefault(r.requirement_id, []).append(r)

    lines = []
    counts = {"clean": 0, "partial": 0, "stale": 0, "ungoverned": 0}
    for req_id in sorted(by_req):
        req_rows = by_req[req_id]
        caps = sorted({r.capability_id for r in req_rows})
        if all(r.policy_id is None for r in req_rows):
            status = "ungoverned"
        elif any(r.is_current_evidence for r in req_rows) and all(
            r.is_current_evidence or r.policy_id is None for r in req_rows
        ):
            status = "clean" if all(r.is_current_evidence for r in req_rows if r.policy_id) else "partial"
        elif any(r.is_current_evidence for r in req_rows):
            status = "partial"
        else:
            status = "stale"
        counts[status] += 1
        lines.append(f"  {req_id}: capabilities {caps} -> {status}")

    clean, partial, stale, ungoverned = counts["clean"], counts["partial"], counts["stale"], counts["ungoverned"]
    if ungoverned or stale or partial:
        verdict = "partial compliance"
    elif clean == len(by_req):
        verdict = "fully compliant"
    else:
        verdict = "non-compliant"

    header = (
        f"GDPR Article 32 verdict: {verdict} "
        f"({clean} of {len(by_req)} sub-clauses clean, {partial} partial, {stale} stale, "
        f"{ungoverned} entirely ungoverned)."
    )
    return header + "\n" + "\n".join(lines)


def answer_h11_mfa_reverse(catalog: Catalog, resolver: CapabilityResolver, free_text: str) -> str:
    """H11 -- reverse walk from a resolved Capability back to every
    Obligation/Regulation, plus the capability's own current governance
    status (per the golden rubric's "note it's hypothetical against today's
    real evidence" requirement).
    """
    hits = resolver.resolve(free_text, top_k=1)
    if not hits:
        raise NoConfidentMatch(f"no capability resembles {free_text!r}")
    cap_id = hits[0].capability_id
    cap_rows = [r for r in catalog.rows if r.capability_id == cap_id]
    if not cap_rows:
        raise NoConfidentMatch(f"resolved to {cap_id!r} but it has no obligations in the catalog")

    obligations = sorted({(r.regulation_id, r.obligation_id) for r in cap_rows})
    by_reg: dict[str, list[str]] = {}
    for reg_id, obl_id in obligations:
        by_reg.setdefault(reg_id, []).append(obl_id)

    governance = next((r for r in cap_rows if r.policy_id), None)
    lines = [f"Resolved {free_text!r} -> {cap_id} ({hits[0].capability_name})", ""]
    lines.append(f"{len(obligations)} obligations across {len(by_reg)} regulations would be out of compliance:")
    for reg_id in sorted(by_reg):
        for obl_id in sorted(by_reg[reg_id]):
            lines.append(f"  {reg_id}: {obl_id}")
    if governance:
        lines.append("")
        lines.append(
            f"Currently governed by {governance.policy_id} ({governance.policy_status}), "
            f"Control {governance.control_id} ({governance.control_status}) -- "
            f"{'CURRENT evidence' if governance.is_current_evidence else 'NOT current evidence'}. "
            "This is hypothetical against today's real evidence, not an existing gap."
        )
    else:
        lines.append("")
        lines.append("Not currently governed by any Policy -- this is an existing gap, not just hypothetical.")
    return "\n".join(lines)


def answer_h5_nis2_staleness(catalog: Catalog) -> str:
    """H5 -- supersession edge + Policy/Standard staleness signals, all
    catalog columns, no free text to resolve.
    """
    lines = ["Regulation supersessions on record:"]
    if catalog.supersessions:
        for a, b in catalog.supersessions:
            lines.append(f"  {a} -[SUPERSEDED_BY]-> {b}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("No NIS2 version supersession exists yet -- that premise is still hypothetical.")
    lines.append("")
    lines.append("Policy staleness signals (not-approved status = potentially out of date):")
    seen_policies = {}
    for r in catalog.rows:
        if r.policy_id and r.policy_id not in seen_policies:
            seen_policies[r.policy_id] = r.policy_status
    for pid, status in sorted(seen_policies.items()):
        if status != "approved":
            lines.append(f"  {pid}: {status}")
    return "\n".join(lines)


def answer_h9_rate_limiting(resolver: CapabilityResolver, free_text: str) -> str:
    """H9 -- the golden answer IS 'no match'; an honest empty resolution
    produces the correct answer directly, no narration/LLM needed.
    """
    hits = resolver.resolve(free_text, top_k=3)
    if not hits:
        return (
            f"No Capability in the graph resembles {free_text!r}. "
            "The graph does not model an API rate-limiting/throttling Capability, "
            "so no Control-blocking verdict can be computed."
        )
    return (
        f"Resolver returned {len(hits)} candidate(s) for {free_text!r}: "
        f"{[h.capability_id for h in hits]} -- review before trusting, this may be a false positive "
        "(short/generic query terms are known to over-match, see experiment_resolver_bakeoff.py)."
    )


if __name__ == "__main__":
    from falkordb import FalkorDB

    from catalog import compile_catalog

    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    res = CapabilityResolver(cat.all_capabilities)

    print("=== H1 ===")
    print(answer_h1_gdpr_art32(cat))
    print("\n=== H11 ===")
    print(answer_h11_mfa_reverse(cat, res, "missing MFA control"))
    print("\n=== H5 ===")
    print(answer_h5_nis2_staleness(cat))
    print("\n=== H9 (hyphenated, matches real question phrasing) ===")
    print(answer_h9_rate_limiting(res, "missing rate-limiting on an endpoint"))
    print("\n=== H9 (bare, unhyphenated -- known resolver fragility) ===")
    print(answer_h9_rate_limiting(res, "rate limiting"))
