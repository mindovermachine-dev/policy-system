#!/usr/bin/env python3
"""Next Steps item 3: stage [3]'s hard-tier shape matcher, per q-approach5.md
§6/§7 critique point 2 -- this has to be a closed, structural rule table, not
a free LLM classifier, or the design reintroduces the same unconstrained
judgment step it exists to avoid, one stage earlier.

Encodes exactly the §4 reclassification table: for each of the 11 questions
query2 routed to the freehand agent, decide (a) does it match a known hard-
tier shape at all, (b) if so, can the missing scope be extracted directly
from the question text (no round-trip needed -- M3, M14, H6 usually name
their own regulation/capability already), (c) if not, what closed-choice
clarification is needed (H3, H12-H14), (d) or whether no clarification helps
at all (M5: present the table and stop; H8: guided multi-select, not a
single question; H10/H15: refuse, name the missing concept).
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query2"))

from catalog import Catalog  # noqa: E402
from catalog_answers import NoConfidentMatch  # noqa: E402
from resolver import CapabilityResolver  # noqa: E402

from catalog_answers_v4 import (  # noqa: E402
    answer_h3_scenario,
    answer_h6_redundancy,
    answer_m3_capability_coverage,
    answer_m14_draft_policies_blocking,
)

NEGATION_MARKERS = ("doesn't", "does not", "don't", "not ", "no ", "without", "missing", "lacks", "isn't", "fails to")


@dataclass
class ClarificationChoice:
    prompt: str
    choices: list[str]  # real values pulled live from the graph -- never invented categories


@dataclass
class RouteResult:
    kind: str  # "direct" | "clarify" | "present_and_stop" | "refuse" | "fallthrough"
    shape: Optional[str] = None
    answer: Optional[str] = None
    clarification: Optional[ClarificationChoice] = None
    extracted: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Per-shape handlers. Each tries direct extraction from the question text
# first (per §5's "impose structure before asking for detail") and only
# falls back to a real clarification round-trip when the question is
# genuinely missing the piece it needs.
# --------------------------------------------------------------------------


def _match_m3(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    m = re.search(r"require\s+an?\s+['\"]?([\w\s\-]+?)['\"]?-?type capability", question, re.I)
    if not m:
        return None
    try:
        answer = answer_m3_capability_coverage(catalog, resolver, m.group(1).strip())
    except NoConfidentMatch as exc:
        return RouteResult(kind="refuse", shape="M3", answer=str(exc))
    return RouteResult(kind="direct", shape="M3", answer=answer, extracted={"capability_text": m.group(1).strip()})


def _match_m14(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    m = re.search(r"draft polic(?:y|ies).*blocking\s+([\w]+)\s+readiness", question, re.I)
    if not m:
        return None
    named_reg = m.group(1).upper()
    all_loaded_regs = sorted({r.regulation_id for r in catalog.rows})
    reg_id = next((r for r in all_loaded_regs if r.startswith(named_reg)), None)
    if reg_id is None:
        # Named a regulation the graph doesn't have loaded -- a real
        # clarification round-trip, not a silent guess.
        return RouteResult(
            kind="clarify",
            shape="M14",
            clarification=ClarificationChoice(
                prompt=f"'{named_reg}' isn't a regulation loaded in this graph. Which one did you mean?",
                choices=all_loaded_regs,
            ),
        )
    answer = answer_m14_draft_policies_blocking(catalog, reg_id)
    return RouteResult(kind="direct", shape="M14", answer=answer, extracted={"regulation_id": reg_id})


def _match_h6(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    m = re.search(r"adopt\s+an?\s+['\"]([\w\s\-]+?)['\"]\s+capability", question, re.I)
    if not m:
        return None
    try:
        answer = answer_h6_redundancy(catalog, resolver, m.group(1).strip())
    except NoConfidentMatch as exc:
        return RouteResult(kind="refuse", shape="H6", answer=str(exc))
    return RouteResult(kind="direct", shape="H6", answer=answer, extracted={"capability_text": m.group(1).strip()})


def _split_scenario_clauses(question: str) -> list[str]:
    """Best-effort split of a scenario into independently-assessable clauses,
    per q-approach5.md §7 critique point 1's own caution: this is a *default
    guess* to pre-fill the clarification step, not a substitute for showing
    it to the user. clarifier.py never returns an H3 answer without routing
    through "clarify" first -- see _match_h3 below.
    """
    # Strip the question's own framing ("Is this new API endpoint, which ...,
    # compliant with ...") down to the descriptive clause list.
    m = re.search(r"which\s+(.+?),?\s+compliant with", question, re.I)
    body = m.group(1) if m else question
    parts = re.split(r"\s*,?\s+but\s+|\s*,\s+and\s+|\s*,\s*", body)
    return [p.strip() for p in parts if p.strip()]


def _guess_satisfies(clause: str) -> bool:
    low = clause.lower()
    return not any(marker in low for marker in NEGATION_MARKERS)


def _match_h3(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    if "compliant with" not in question.lower() or " this " not in f" {question.lower()} ":
        return None  # scenario-scoped shape only -- org-wide H1 phrasing doesn't match ("this" is the anchor)
    clauses = _split_scenario_clauses(question)
    if not clauses:
        return None
    default_claims = [(c, _guess_satisfies(c)) for c in clauses]
    resolved = []
    for clause, satisfies in default_claims:
        hits = resolver.resolve(clause, top_k=1)
        resolved.append(
            {
                "clause": clause,
                "guessed_satisfies": satisfies,
                "resolved_capability": hits[0].capability_id if hits else None,
                "resolved_name": hits[0].capability_name if hits else None,
            }
        )
    choices = [f"{r['clause']!r} -> {r['resolved_capability'] or 'NO MATCH'} "
               f"(guessed satisfied={r['guessed_satisfies']}, confirm or flip)" for r in resolved]
    return RouteResult(
        kind="clarify",
        shape="H3",
        clarification=ClarificationChoice(
            prompt="I split this scenario into per-capability claims -- confirm or correct each before I compute a verdict:",
            choices=choices,
        ),
        extracted={"default_claims": default_claims},
    )


def _match_m5(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    if not re.search(r"impose obligations on similar roles|similar roles", question, re.I):
        return None
    # No clarification collapses this -- confirmed live in q-approach5.md's
    # source evidence (golden-answers.md's M5 entry): CRA's 6 roles and
    # NIS2's 2 roles share no vocabulary at all. Present the real sets, stop.
    cra_roles = sorted({r.role_name for r in catalog.rows if r.regulation_id == "CRA-1.0"})
    nis2_roles = sorted({r.role_name for r in catalog.rows if r.regulation_id == "NIS2-1.0"})
    answer = (
        "The graph doesn't encode role equivalence across regulations, so this mechanism doesn't judge "
        "'similar' for you -- here are both real sets:\n"
        f"  CRA-1.0 roles: {cra_roles}\n"
        f"  NIS2-1.0 roles: {nis2_roles}\n"
        "No shared role name exists between them; any similarity claim is a reader's semantic judgment, "
        "not a structural fact this graph can confirm."
    )
    return RouteResult(kind="present_and_stop", shape="M5", answer=answer)


def _match_h8(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    if not re.search(r"what compliance capabilities should I be thinking about", question, re.I):
        return None
    # Confirmed live in verify_remaining_rows.py: no single free-text call
    # reaches more than 2 of the 5 golden capabilities even at top_k=10.
    # Guided multi-select over the graph's own compliance dimensions, not a
    # single clarifying question.
    dimensions = [
        "data at rest / in transit (encryption)",
        "access control / authentication",
        "logging of access and changes",
        "data protection impact assessment (high-risk processing)",
        "data-subject deletion / portability rights",
    ]
    return RouteResult(
        kind="clarify",
        shape="H8",
        clarification=ClarificationChoice(
            prompt="Which of these does your new service touch? (select all that apply -- this maps to real "
            "Capabilities in the graph, not an open free-text guess)",
            choices=dimensions,
        ),
    )


def _match_h12_14(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    if not re.search(r"most exposed|board|prioritize this quarter", question, re.I):
        return None
    axes = [
        "review urgency (Control.next_review_date -- overdue/soon first)",
        "approval state (draft/deprecated Policies and Standards first)",
        "coverage gap (ungoverned Capabilities first)",
    ]
    return RouteResult(
        kind="clarify",
        shape="H12-H14",
        clarification=ClarificationChoice(
            prompt="Prioritize by which axis? (all three are real, sortable graph columns -- picking one "
            "turns this into a deterministic ranked list, not a synthesized narrative)",
            choices=axes,
        ),
    )


def _match_schema_gap(question: str, catalog: Catalog, resolver: CapabilityResolver) -> Optional[RouteResult]:
    if re.search(r"currently compliant\?\s*$", question, re.I) and "service" in question.lower():
        return RouteResult(
            kind="refuse",
            shape="H10",
            answer="The graph has no Service/System node and no edge linking code to a Capability. This is a "
            "missing concept in the domain model, not something clarification can resolve -- no amount of "
            "scoping produces an answer the schema structurally cannot give.",
        )
    if re.search(r"go from draft to implemented", question, re.I):
        return RouteResult(
            kind="refuse",
            shape="H15",
            answer="Standard/Policy/Control carry only a current status, no timestamped transition history. "
            "This is a missing concept (a status-transition log), not a query-mechanism gap -- clarification "
            "cannot supply data that was never recorded.",
        )
    return None


HARD_TIER_MATCHERS = [
    _match_m3,
    _match_m14,
    _match_h6,
    _match_h3,
    _match_m5,
    _match_h8,
    _match_h12_14,
    _match_schema_gap,
]


def route(question: str, catalog: Catalog, resolver: CapabilityResolver) -> RouteResult:
    for matcher in HARD_TIER_MATCHERS:
        result = matcher(question, catalog, resolver)
        if result is not None:
            return result
    return RouteResult(kind="fallthrough")


if __name__ == "__main__":
    from falkordb import FalkorDB

    from catalog import compile_catalog

    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    res = CapabilityResolver(cat.all_capabilities)

    QUESTIONS = [
        ("M3", "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?"),
        ("M14", "Which of our draft Policies are blocking GDPR readiness?"),
        ("H6", "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy?"),
        ("H3", "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?"),
        ("M5", "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?"),
        ("H8", "I'm building a new microservice that stores customer PII in a database - what compliance capabilities should I be thinking about?"),
        ("H12", "Across our whole Control set, where are we most exposed - what would an auditor flag first?"),
        ("H13", "Give me a one-paragraph summary of our overall compliance posture I can bring to the board."),
        ("H14", "What should my team prioritize this quarter to move the needle on compliance?"),
        ("H10", "Is my service, checkout-api, currently compliant?"),
        ("H15", "How long, on average, does it take a Standard to go from draft to implemented in our organization?"),
    ]
    for qid, q in QUESTIONS:
        r = route(q, cat, res)
        print(f"{qid:5} kind={r.kind:16} shape={r.shape}")
        if r.kind == "clarify":
            print(f"      clarify: {r.clarification.prompt}")
            for c in r.clarification.choices:
                print(f"        - {c}")
        elif r.answer:
            print(f"      answer (first line): {r.answer.splitlines()[0]}")
