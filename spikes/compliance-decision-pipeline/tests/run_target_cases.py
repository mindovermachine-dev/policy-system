# © 2026 Cartman ApS. All rights reserved.
"""End-to-end demonstration: wires Stage 1 + Stage 2 + Stage 4 into
pipeline.compose.compose_output() for every target case that has a real,
validated Stage 4 mechanism (PROGRESS.md's Build status table) and prints
the resulting three-block (A)/(B)/(C) output.

This is PROGRESS.md's "Next action (b)" -- not a new mechanism, a
demonstration that the mechanisms already validated in isolation
(tests/test_stage1.py, test_stage4.py) compose into the actual output
contract README.md specifies. Two variants per case where one exists: the
failing transcript's answer (must produce a flagged/not-verified output)
and the golden-correct answer (must produce a confident output) -- the
same must-flag/must-not-flag discipline Setup step 4 used for the
mechanisms themselves, now applied at the composed-output level.

Deliberately excludes AU-M4: a real Stage 1 target case, not a Stage 4
one -- composing an output for it would mean a fitness gate with zero
checks run, i.e. a vacuous pass. Silently doing that would hide the gap
PROGRESS.md documents rather than show it. SEC-H4, SA-H1, SA-H2, and CO-H2
were the same kind of gap as of the prior session; each now has a real,
validated Stage 4 mechanism behind it (see fitness.py's module docstring
for what changed) and is included below.

Requires FalkorDB reachable at localhost:6379 (same as test_stage4.py).

Run: /usr/bin/python3 tests/run_target_cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import fitness  # noqa: E402
from pipeline.alias_table import run_stage1  # noqa: E402
from pipeline.compose import compose_output  # noqa: E402
from pipeline.structural import run_stage2  # noqa: E402
from pipeline.types import FitnessResult  # noqa: E402
from tests.fixtures import (  # noqa: E402
    CO_H2_OBLIGATIONS,
    CO_H2_REQUIREMENT_IDS,
    DATA_ENCRYPTION_CAPABILITY,
    DATA_ENCRYPTION_OBLIGATIONS,
    DEPRECATED_PAST_DUE_CONTROL,
    INCIDENT_POLICY_CONTROLS,
    MAX_FANOUT_CAPABILITY,
    MAX_FANOUT_COUNT,
    MFA_OBLIGATIONS,
    REFERENCE_DATE,
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
_SEC_H1_TEXT = "Which obligations require multi-factor authentication (MFA)?"
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
    fitness_result = FitnessResult(question_id=question_id, checks=fitness_checks)
    return compose_output(question_id, question_type, stage1, stage2, fitness_result, answer)


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
            "SEC-H1", "B", _SEC_H1_TEXT,
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
                "gate_passed": not any(c["flagged"] for c in output.verification_data["fitness_checks"]),
                "A_confidence_statement": output.confidence_statement,
                "B_answer": output.answer,
                "C_verification_data": output.verification_data,
            }
        )
    print(json.dumps(report, indent=2))

    n_complete = sum(1 for r in report if r["is_complete"])
    n_gate_failed = sum(1 for r in report if not r["gate_passed"])
    print(f"\n{len(report)} scenarios, {n_complete} three-block-complete, {n_gate_failed} gate-failed (expected: the *-failing ones).", file=sys.stderr)


if __name__ == "__main__":
    main()
