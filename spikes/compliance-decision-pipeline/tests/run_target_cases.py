# © 2026 Cartman ApS. All rights reserved.
"""End-to-end demonstration: wires Stage 1 + Stage 2 + Stage 3 + Stage 4
into pipeline.compose.compose_output() for every target case that has a
real, validated Stage 4 mechanism (PROGRESS.md's Build status table) and
prints the resulting three-block (A)/(B)/(C) output.

This is PROGRESS.md's "Next action (b)" -- not a new mechanism, a
demonstration that the mechanisms already validated in isolation
(tests/test_stage1.py, test_stage4.py) compose into the actual output
contract README.md specifies. Two variants per case where one exists: the
failing transcript's answer (must produce a flagged/not-verified output)
and the golden-correct answer (must produce a confident output) -- the
same must-flag/must-not-flag discipline Setup step 4 used for the
mechanisms themselves, now applied at the composed-output level.

AU-M4 is now included, not excluded: it was previously left out because
composing it with zero Stage 4 checks would be a vacuous pass, and
silently doing that would have hidden the gap rather than shown it. Stage
3's routing (pipeline/routing.py) now makes that gap visible on purpose --
AU-M4 needs a check (`stale_chain_strict_reading`) that doesn't exist yet,
so routing it correctly produces a flagged, not-verified result instead of
a false confident one. See routing.py's module docstring.

EM-M4 is also now included: `pipeline.fitness.check_evidence_gap_root_cause`
(later session) closed the "Granularity precision" success criterion's
partial status -- EM-M4's mismatch is a root-cause (governance vs.
engineering) classification, not a counting-unit one like EM-E3, so it
needed its own mechanism rather than forcing a fit onto
check_entity_type_match. See fitness.py's module docstring and
PROGRESS.md's "Stage 4 -- root-cause classification" section.

CO-M2 is included too, from a live held-out generalization audit
(PROGRESS.md): it's one of exactly 3 `blind_questions.tsv` failures never
used to design any mechanism, run blind against the already-built pipeline
first -- and slipped through uncaught (check_existence only detects
fabrication, not omission). `check_completeness` was then built using
CO-M2 as direct design reference, which is why it carries an explicit
non-held-out caveat unlike every other scenario here.

Requires FalkorDB reachable at localhost:6379 (same as test_stage4.py).

Run: /usr/bin/python3 tests/run_target_cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import fitness, ps_client  # noqa: E402
from pipeline.alias_table import run_stage1  # noqa: E402
from pipeline.compose import compose_output  # noqa: E402
from pipeline.routing import route_question  # noqa: E402
from pipeline.structural import run_stage2  # noqa: E402
from pipeline.types import FitnessResult  # noqa: E402
from tests.fixtures import (  # noqa: E402
    CO_H2_OBLIGATIONS,
    CO_H2_REQUIREMENT_IDS,
    CO_M2_CONFIDENCE_QUERY,
    DATA_ENCRYPTION_CAPABILITY,
    DATA_ENCRYPTION_OBLIGATIONS,
    DEPRECATED_PAST_DUE_CONTROL,
    EM_M4_CLINICAL_CAPABILITY,
    EM_M4_INCIDENT_CAPABILITIES,
    INCIDENT_POLICY_CONTROLS,
    MAX_FANOUT_CAPABILITY,
    MAX_FANOUT_COUNT,
    MFA_OBLIGATIONS,
    PM_M3_ROPA_CAPABILITY,
    REFERENCE_DATE,
    SA_M2_HELVEX_CRA_INTERSECTION_QUERY,
    SBOM_CAPABILITY,
    SECURITY_LOGGING_CAPABILITY,
    SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION,
    SEC_H4_OVERCLAIMED_MFA_OBLIGATION,
    TRUE_OVERDUE_CONTROL,
)

_SEC_M2_TEXT = "Which checks are overdue for review right now — not just due soon?"
_SEC_M4_TEXT = (
    "Which checks come due for review before the end of August 2026, "
    "and which are already overdue?"
)
_AU_H4_TEXT = (
    "If our log-retention check turns out to have failed, which "
    "regulatory requirements does that undermine?"
)
_EM_E3_TEXT = "How many of our GDPR evidence chains would currently hold up in an audit?"
_SEC_E1_TEXT = "Which Controls implement the incident/vulnerability response policy?"
_SEC_H1_TEXT = (
    "If an attacker exploited a missing MFA check today, which regulatory "
    "duties across CRA/NIS2/GDPR would we be in breach of?"
)
_SA_H1_TEXT = (
    "If we adopt a 'Software Bill of Materials' capability, which existing "
    "CRA/NIS2 obligations would it newly satisfy, and where are we already "
    "redundantly covered?"
)
_SA_H2_TEXT = (
    "If a single capability of ours fails, which failure endangers the "
    "most obligations -- and is that even the right way to think about "
    "criticality?"
)
_CO_H2_TEXT = (
    "We found a vulnerability in an open-source component we bundle -- is "
    "shipping our own fix enough, or does the CRA make us do more?"
)
_SEC_H4_TEXT = (
    "If the Encryption-at-Rest check fails its review on August 15, which "
    "regulatory duties does that put at risk?"
)
_AU_M4_TEXT = (
    "Which GDPR articles currently have only stale requirement-to-control "
    "evidence chains, and why?"
)
_EM_M4_TEXT = (
    "How much of our GDPR evidence problem is a governance problem versus "
    "an engineering problem?"
)
_CO_M2_TEXT = (
    "Which of our extracted regulatory duties have the shakiest provenance "
    "confidence and should get a human review?"
)
_SA_E3_TEXT = "Do NIS2 or GDPR need our SBOM capability for anything today?"
_SA_M2_TEXT = "What capabilities does our internal Helvex SOP have in common with the CRA?"
_PM_M3_TEXT = (
    "GDPR requires records of processing and DPIAs — do our policies "
    "actually cover both duties?"
)
_AU_H2_TEXT = (
    "Trace the CRA's actively-exploited-vulnerability reporting duty from "
    "the regulation text all the way into our internal governance — does "
    "the trail reach a check that's actually running?"
)

_SEC_E1_QUERY = (
    "MATCH (p:Policy {id: 'pol_incident_vulnerability_response_policy_9de859'})"
    "-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(c:Control) RETURN c.id"
)
_SEC_H1_QUERY = (
    "MATCH (o:Obligation)-[:REQUIRES]->"
    "(c:Capability {id: 'cap_access_control_authentication_151816'}) RETURN o.id"
)
_CO_H2_QUERY = (
    "MATCH (r:Requirement)-[:SATISFIED_BY]->(o:Obligation) WHERE r.id IN ["
    + ", ".join(f"'{rid}'" for rid in CO_H2_REQUIREMENT_IDS)
    + "] RETURN o.id"
)
_SEC_H4_QUERY = (
    "MATCH (o:Obligation)-[:REQUIRES]->"
    f"(c:Capability {{id: '{DATA_ENCRYPTION_CAPABILITY}'}}) RETURN o.id"
)


def _run(question_id: str, question_type: str, text: str, fitness_checks: list, answer: str):
    stage1 = run_stage1(question_id, text)
    stage2 = run_stage2(question_id, text)
    routing = route_question(stage1, stage2, question_type)
    fitness_result = FitnessResult(question_id=question_id, checks=fitness_checks)
    return compose_output(question_id, question_type, stage1, stage2, fitness_result, answer, routing)


def build_scenarios() -> list:
    scenarios = []

    # SEC-M2 / SEC-M4 (type B) -- rule check: overdue must exclude deprecated.
    for qid, text in (("SEC-M2", _SEC_M2_TEXT), ("SEC-M4", _SEC_M4_TEXT)):
        failing_set = {TRUE_OVERDUE_CONTROL, DEPRECATED_PAST_DUE_CONTROL}
        scenarios.append((
            f"{qid}-failing",
            _run(
                qid, "B", text,
                [fitness.check_overdue_excludes_deprecated(REFERENCE_DATE, failing_set)],
                f"Overdue: {sorted(failing_set)} (the second is deprecated, review lapsed anyway).",
            ),
        ))
        golden_set = {TRUE_OVERDUE_CONTROL}
        scenarios.append((
            f"{qid}-golden",
            _run(
                qid, "B", text,
                [fitness.check_overdue_excludes_deprecated(REFERENCE_DATE, golden_set)],
                f"Overdue: {sorted(golden_set)}.",
            ),
        ))

    # AU-H4 (type D) -- scope-match: regulation-routing.
    scenarios.append((
        "AU-H4-failing",
        _run(
            "AU-H4", "D", _AU_H4_TEXT,
            [fitness.check_regulation_scope(SECURITY_LOGGING_CAPABILITY, {"CRA-1.0", "NIS2-1.0", "GDPR-1.0"})],
            "This undermines CRA, NIS2, and GDPR duties.",
        ),
    ))
    scenarios.append((
        "AU-H4-golden",
        _run(
            "AU-H4", "D", _AU_H4_TEXT,
            [fitness.check_regulation_scope(SECURITY_LOGGING_CAPABILITY, {"CRA-1.0", "HELVEX-SOP-1.0"})],
            "This undermines CRA (and the internal Helvex SOP); NIS2 and GDPR are not routed through this capability.",
        ),
    ))

    # EM-E3 (type C) -- entity-type cross-check.
    scenarios.append((
        "EM-E3-failing",
        _run(
            "EM-E3", "C", _EM_E3_TEXT,
            [fitness.check_entity_type_match("chain", "control")],
            "3 of 57 controls would hold up.",
        ),
    ))
    scenarios.append((
        "EM-E3-golden",
        _run(
            "EM-E3", "C", _EM_E3_TEXT,
            [fitness.check_entity_type_match("chain", "chain")],
            "31 of 57 chains would hold up.",
        ),
    ))

    # SEC-E1 / SEC-H1 (type B) -- existence grounding, golden answers only
    # (these two dev-v2b failures were incompleteness/mis-citation, not a
    # wrong claimed set -- see PROGRESS.md; the correct sets are what's
    # demonstrated clearing the gate here).
    scenarios.append((
        "SEC-E1-golden",
        _run(
            "SEC-E1", "B", _SEC_E1_TEXT,
            [fitness.check_existence(INCIDENT_POLICY_CONTROLS, _SEC_E1_QUERY)],
            f"Controls: {sorted(INCIDENT_POLICY_CONTROLS)}.",
        ),
    ))
    scenarios.append((
        "SEC-H1-golden",
        _run(
            # type D, not B -- see PROGRESS.md's text-audit finding: this
            # is dev-questions.md's actual verbatim wording (an "if an
            # attacker exploited X today" hypothetical-chain shape), not
            # the plain enumeration paraphrase used before this session.
            "SEC-H1", "D", _SEC_H1_TEXT,
            [fitness.check_existence(MFA_OBLIGATIONS, _SEC_H1_QUERY)],
            f"Obligations: {sorted(MFA_OBLIGATIONS)}.",
        ),
    ))

    # SA-H1 (type B) -- scope-match: regulation-routing, reused for a
    # different capability (SBOM). Zero NIS2/GDPR redundant coverage today.
    scenarios.append((
        "SA-H1-failing",
        _run(
            "SA-H1", "B", _SA_H1_TEXT,
            [fitness.check_regulation_scope(SBOM_CAPABILITY, {"NIS2-1.0"})],
            "We're already redundantly covered by an existing NIS2 obligation.",
        ),
    ))
    scenarios.append((
        "SA-H1-golden",
        _run(
            "SA-H1", "B", _SA_H1_TEXT,
            [fitness.check_regulation_scope(SBOM_CAPABILITY, {"CRA-1.0"})],
            "Only CRA's SBOM obligation requires this capability today; no NIS2/GDPR redundant coverage exists yet.",
        ),
    ))

    # SA-H2 (type G) -- ranking grounding: the actual maximum-fanout capability and count.
    scenarios.append((
        "SA-H2-failing",
        _run(
            "SA-H2", "G", _SA_H2_TEXT,
            [fitness.check_fanout_maximum("cap_security_incident_reporting_449fa4", 30)],
            "cap_security_incident_reporting_449fa4 endangers the most obligations (30) if it fails.",
        ),
    ))
    scenarios.append((
        "SA-H2-golden",
        _run(
            "SA-H2", "G", _SA_H2_TEXT,
            [fitness.check_fanout_maximum(MAX_FANOUT_CAPABILITY, MAX_FANOUT_COUNT)],
            f"{MAX_FANOUT_CAPABILITY} endangers the most obligations ({MAX_FANOUT_COUNT}) by raw fan-out -- "
            "but fan-out is blast radius, not criticality; a capability with fewer obligations can still be "
            "the sole point of failure for a high-severity duty.",
        ),
    ))

    # CO-H2 (type B) -- existence grounding: the real "beyond our own fix" obligation set.
    scenarios.append((
        "CO-H2-failing",
        _run(
            "CO-H2", "B", _CO_H2_TEXT,
            [fitness.check_existence(
                (CO_H2_OBLIGATIONS - {"obl_report_actively_exploited_vulnerabilities_8fd384"})
                | {"obl_does_not_exist_deadbeef"},
                _CO_H2_QUERY,
            )],
            "Shipping our own fix is enough; no further CRA duties apply.",
        ),
    ))
    scenarios.append((
        "CO-H2-golden",
        _run(
            "CO-H2", "B", _CO_H2_TEXT,
            [fitness.check_existence(CO_H2_OBLIGATIONS, _CO_H2_QUERY)],
            f"Obligations: {sorted(CO_H2_OBLIGATIONS)}.",
        ),
    ))

    # SEC-H4 (type D) -- existence grounding scoped to the failing control's own capability.
    scenarios.append((
        "SEC-H4-failing",
        _run(
            "SEC-H4", "D", _SEC_H4_TEXT,
            [fitness.check_existence(
                {
                    "obl_apply_pseudonymisation_and_encryption_as_controller_fc1f7e",
                    SEC_H4_OVERCLAIMED_MFA_OBLIGATION,
                    SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION,
                },
                _SEC_H4_QUERY,
            )],
            "This puts GDPR Art 32(1)(a), the NIS2 MFA duty, and the security-logging duty at risk.",
        ),
    ))
    scenarios.append((
        "SEC-H4-golden",
        _run(
            "SEC-H4", "D", _SEC_H4_TEXT,
            [fitness.check_existence(DATA_ENCRYPTION_OBLIGATIONS, _SEC_H4_QUERY)],
            "GDPR Art 32(1)(a) is the primary casualty; CRA's encryption duty is hedged (redundant standard-level backing).",
        ),
    ))

    # EM-M4 (type C) -- root-cause classification, not entity-type
    # cross-check (that's EM-E3's shape, a different dimension). Golden
    # names the Clinical-draft capability "governance" and the three
    # Incident-v2 capabilities "engineering" (RUNBOOK.md's "Clinical-draft
    # 10 vs incident-v2 10" note, reproduced live -- see fitness.py). The
    # failing scenario reproduces "framed at a different sub-grouping than
    # golden" by mis-attributing one Incident-v2 capability (an active,
    # tracked engineering build) as a governance failure instead.
    incident_ids = sorted(EM_M4_INCIDENT_CAPABILITIES)
    scenarios.append((
        "EM-M4-failing",
        _run(
            "EM-M4", "C", _EM_M4_TEXT,
            [
                fitness.check_evidence_gap_root_cause(EM_M4_CLINICAL_CAPABILITY, "governance"),
                fitness.check_evidence_gap_root_cause(incident_ids[0], "governance"),  # mis-attributed
                fitness.check_evidence_gap_root_cause(incident_ids[1], "engineering"),
                fitness.check_evidence_gap_root_cause(incident_ids[2], "engineering"),
            ],
            "16 of our GDPR evidence gaps are governance problems, 10 are engineering problems.",
        ),
    ))
    scenarios.append((
        "EM-M4-golden",
        _run(
            "EM-M4", "C", _EM_M4_TEXT,
            [
                fitness.check_evidence_gap_root_cause(EM_M4_CLINICAL_CAPABILITY, "governance"),
                fitness.check_evidence_gap_root_cause(incident_ids[0], "engineering"),
                fitness.check_evidence_gap_root_cause(incident_ids[1], "engineering"),
                fitness.check_evidence_gap_root_cause(incident_ids[2], "engineering"),
            ],
            "10 of our GDPR evidence gaps trace to the Clinical Data Integrity policy (still draft -- "
            "a governance gap); 10 trace to the Incident/Vulnerability Response policy's v2 automation "
            "(already implemented at v1, so an engineering-in-progress gap, not a governance one).",
        ),
    ))

    # CO-M2 (type B) -- completeness grounding, the mechanism the live
    # held-out audit motivated (PROGRESS.md). Golden and the actual
    # recorded failing set (missing 9 of the 21-item 0.80 confidence band)
    # are both derived live -- see fitness.py's module docstring for the
    # caveat: this case was used to design check_completeness, not just
    # validate it, so it's spent as held-out data going forward.
    co_m2_golden = {r[0] for r in ps_client.cypher(CO_M2_CONFIDENCE_QUERY)["rows"]}
    co_m2_band_080 = sorted({
        r[0] for r in ps_client.cypher("MATCH (o:Obligation) WHERE o.confidence = 0.80 RETURN o.id")["rows"]
    })
    co_m2_band_075 = {
        r[0] for r in ps_client.cypher("MATCH (o:Obligation) WHERE o.confidence = 0.75 RETURN o.id")["rows"]
    }
    co_m2_failing = co_m2_band_075 | set(co_m2_band_080[:12])
    scenarios.append((
        "CO-M2-failing",
        _run(
            "CO-M2", "B", _CO_M2_TEXT,
            [fitness.check_completeness(co_m2_failing, CO_M2_CONFIDENCE_QUERY)],
            f"{len(co_m2_failing)} obligations have shaky provenance confidence (0.75/0.80 band) and need human review.",
        ),
    ))
    scenarios.append((
        "CO-M2-golden",
        _run(
            "CO-M2", "B", _CO_M2_TEXT,
            [fitness.check_completeness(co_m2_golden, CO_M2_CONFIDENCE_QUERY)],
            f"{len(co_m2_golden)} obligations have shaky provenance confidence (0.75/0.80 band) and need human review.",
        ),
    ))

    # SA-E3, SA-M2, PM-M3 -- precision tests (PROGRESS.md, "would running
    # more held-out questions through be meaningful"): passing, never-
    # composed held-out questions that reuse an entity an existing
    # mechanism already covers. Golden-only -- no failing variant, since
    # these are correct transcripts, not recorded failures.
    scenarios.append((
        "SA-E3-golden",
        _run(
            "SA-E3", "B", _SA_E3_TEXT,
            [fitness.check_regulation_scope(SBOM_CAPABILITY, {"CRA-1.0"})],
            "No -- NIS2 and GDPR don't require the SBOM capability today; only CRA does.",
        ),
    ))
    scenarios.append((
        "SA-M2-golden",
        _run(
            "SA-M2", "B", _SA_M2_TEXT,
            [fitness.check_existence({"cap_security_logging_c4d9e2"}, SA_M2_HELVEX_CRA_INTERSECTION_QUERY)],
            "cap_security_logging_c4d9e2 -- the only capability shared between the Helvex SOP and CRA.",
        ),
    ))
    scenarios.append((
        "PM-M3-golden",
        _run(
            "PM-M3", "B", _PM_M3_TEXT,
            [
                fitness.check_evidence_gap_root_cause(EM_M4_CLINICAL_CAPABILITY, "governance"),
                fitness.check_evidence_gap_root_cause(PM_M3_ROPA_CAPABILITY, "governance"),
            ],
            "No -- DPIA is draft-only and Art. 30 records of processing is ungoverned; neither duty is actually covered.",
        ),
    ))

    # AU-H2 (type D) -- regression scenario for the vacuous-pass hole this
    # very question exposed (PROGRESS.md): before compose.py's enforcement
    # was keyed on routing.path instead of on mandatory_check_names being
    # non-empty, composing this with zero Stage 4 checks produced a
    # confident "this is correct" -- AU-H2 is type D but not the
    # hypothetical-chain shape, so routing.py names no specific mandatory
    # check for it, and the old condition treated "no check named" as "no
    # check required". Now correctly fails closed on zero checks, same as
    # AU-M4-unbuilt-check, for a related but distinct reason (no check
    # exists AT ALL for this route, vs. AU-M4 which names one specific
    # unbuilt check).
    scenarios.append((
        "AU-H2-zero-checks",
        _run(
            "AU-H2", "D", _AU_H2_TEXT,
            [],
            "Yes, the trail reaches a live, running check.",
        ),
    ))

    # AU-M4 (type B) -- not a Stage 4 mechanism demonstration; a Stage 3
    # enforcement demonstration. "stale" disambiguation has no built check
    # (see routing.py), so this composes with zero Stage 4 checks -- before
    # Stage 3's mandatory-check enforcement existed, that would have been a
    # silent vacuous pass. Now it correctly comes back flagged.
    scenarios.append((
        "AU-M4-unbuilt-check",
        _run(
            "AU-M4", "B", _AU_M4_TEXT,
            [],
            "GDPR articles 32.4, 37, and 38 currently have only stale evidence chains.",
        ),
    ))

    return scenarios


def main() -> None:
    scenarios = build_scenarios()
    report = []
    for label, output in scenarios:
        report.append(
            {
                "label": label,
                "question_id": output.question_id,
                "is_complete": output.is_complete,
                # Not just "no check flagged" -- a mandatory-check-not-performed
                # result (Stage 3, e.g. AU-M4) has zero flagged checks yet still
                # isn't a pass. answer's [FLAGGED ...] prefix is compose.py's own
                # single source of truth for "this is not a verified result".
                "gate_passed": not output.answer.startswith("[FLAGGED"),
                "A_confidence_statement": output.confidence_statement,
                "B_answer": output.answer,
                "C_verification_data": output.verification_data,
            }
        )
    print(json.dumps(report, indent=2))

    n_complete = sum(1 for r in report if r["is_complete"])
    n_gate_failed = sum(1 for r in report if not r["gate_passed"])
    print(
        f"\n{len(report)} scenarios, {n_complete} three-block-complete, {n_gate_failed} gate-failed "
        "(expected: the *-failing ones, plus AU-M4-unbuilt-check -- Stage 3's mandatory-check "
        "enforcement correctly fails closed on a check that doesn't exist yet).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
