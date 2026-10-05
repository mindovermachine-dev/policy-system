"""#191 Slice 4 (recommended, non-gating): live-LLM evidence for the recalibrated
capability-reuse verification prompt (S1+S2's `CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT`).

PLAN.md §4 S4 plus CHANGES.md's deltas (CHANGES.md wins: row M3, m1, Appendix A4-A6).
Fake-caller tests in `test_derivation.py`/`test_prompts.py` only prove the recalibrated
guidance is the system prompt recorded at the LLM boundary (PLAN.md G7/G8); they script the
verdict. Only a real model call shows the recalibrated wording now accepts the real #187 DORA
subsumption (G3) and domain-qualifier (G4) cases while still rejecting genuinely distinct
pairs. This is one targeted pytest of verifier calls, not the "further live DORA re-ingest"
the issue excludes.

`@pytest.mark.llm_live`, no `falkordb_live` marker: no graph is read or written here, only
`_build_reuse_verification_messages` -> `route_completion` -> `parse_capability_reuse_verdict`
against the real `derivation.py`/`prompts.py` functions (PLAN.md §4 S4; CHANGES.md row 4).

Four GATING cases (parametrized, CHANGES.md "Final slice sequence" item 4):
  - `subsumption_eu_reporting_ict_incident` -> accept (PLAN.md G3; data == test_derivation.py's
    S1 constants `_EU_REPORTING_NAME`/`_EU_REPORTING_DESCRIPTION`/`_ICT_INCIDENT_OBLIGATION_TEXT`).
  - `qualifier_ict_operational_risk` -> accept (PLAN.md G4; data == test_derivation.py's S2
    constants `_ICT_OP_RISK_NAME`/`_ICT_OP_RISK_DESCRIPTION`/`_OP_RISK_OBLIGATION_TEXT`).
  - `distinct_incident_reporting_vs_client_notification` -> reject (PLAN.md G5a; replaces the
    disputed BCP/DR pair in the gating set per CHANGES.md row M3).
  - `distinct_generic_internal_controls_vs_nis_security` -> reject (CHANGES.md Appendix A4;
    M2's over-generality guard, real-model tested against #187's pre-fix "Internal Controls"
    catch-all bundling, AC-BI-013).

One INFORMATIONAL (non-gating) case per CHANGES.md Appendix A5:
  `test_reuse_verifier_on_contested_bcp_dr_pair_reports_verdict` only checks the response
  parses and prints the verdict for the tracker (run with `-s` to see it); its ground truth is
  disputed (ANALYSIS_FRAGMENTATION.md:53 "distinct" vs the faithful ablation judge's
  "should_have_reused"), so it is not asserted against an expected verdict.

Per PLAN.md's convention (mirrors `test_prompts_live.py`): on any GATING failure, do not patch
the prompt in this slice -- report back to the orchestrator and Critique with the verbatim
verdict JSON. LLM output is nondeterministic and this is a single sample per case, so a flaky
result must be reported as such, not silently retried or re-tuned away.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from ps_service.domain_mapper.derivation import (
    _build_reuse_verification_messages,  # pyright: ignore[reportPrivateUsage]  # test drives this module-internal helper directly (see module docstring)
)
from ps_service.domain_mapper.errors import DomainMapperDerivationError
from ps_service.domain_mapper.prompts import parse_capability_reuse_verdict
from ps_service.llm_interface.completion import route_completion

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter

pytestmark = [pytest.mark.llm_live]

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_MODEL", ...)` for every
# test -- mirrors `test_prompts_live.py`'s established pattern exactly, for the same reason:
# this live test's whole point is to use the real configured model, so it must be read before
# that fixture strips it.
_LLM_INTERFACE_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")

_SKIPIF_REASON = "requires .env sourced (PS_LLMINTERFACE_MODEL, AZURE_API_KEY, AZURE_API_BASE)"

# ---------------------------------------------------------------------------
# GATING cases. Each tuple: (obligation_text, capability_name, capability_description,
# obligation_node_id, capability_node_id, expected_verdict). Ids/text are copied verbatim from
# PLAN.md/CHANGES.md and from test_derivation.py's S1/S2 module constants -- never paraphrased.

_SUBSUMPTION_CASE = (
    # PLAN.md G3: real #187 DORA subsumption case, dora-reingest-187.jsonl:1053.
    # Same data as test_derivation.py's S1 constants `_ICT_INCIDENT_OBLIGATION_TEXT` /
    # `_EU_REPORTING_NAME` / `_EU_REPORTING_DESCRIPTION`.
    #
    # m1 (CHANGES.md Appendix A6): "Known uncertainty: the description reaches 'EU
    # authorities' while the duty targets 'the Competent Authority' (usually national under
    # DORA); a live reject citing audience is an expected-risk outcome to report (#191 FLAWS
    # m1), not a prompt patch."
    "Report Major ICT-Related Incidents to the Competent Authority",
    "EU Regulatory Reporting",
    (
        "Ability to submit required legal or regulatory notifications and reports to EU "
        "authorities in compliance with applicable rules."
    ),
    "obl_report_major_ict_related_incidents_to_the_competent_authority_db689a",
    "cap_eu_regulatory_reporting_16d668",
    "accept",
)

_QUALIFIER_CASE = (
    # PLAN.md G4: real #187 DORA domain-qualifier case, dora-reingest-187.jsonl:1904. Same
    # data as test_derivation.py's S2 constants `_OP_RISK_OBLIGATION_TEXT` /
    # `_ICT_OP_RISK_NAME` / `_ICT_OP_RISK_DESCRIPTION`. Direction follows the raw #187 log
    # (PLAN.md G4), not the issue prose, per user decision C1(b), CHANGES.md.
    #
    # M1 residual risk (CHANGES.md Appendix A5): "Accept is the user-signed design choice
    # (#191 C1(b)); the #187 ablation judge rated this pair correctly_distinct. A live reject
    # is a real failure to escalate, not to re-tune here."
    (
        "Identify and Minimise Operational Risks Through Appropriate Systems, Controls, and "
        "Procedures"
    ),
    "ICT Operational Risk Management",
    (
        "Ability to identify, assess, monitor, and control operational risks arising from "
        "ICT systems, processes, and services."
    ),
    (
        "obl_identify_and_minimise_operational_risks_through_appropriate_systems_controls_and_"
        "procedures_d3d334"
    ),
    "cap_ict_operational_risk_management_53bcf1",
    "accept",
)

_DISTINCT_CLIENT_NOTIFICATION_CASE = (
    # PLAN.md G5a: real #187 DORA genuinely-distinct reject, dora-reingest-187.jsonl:1061.
    # Verifier correctly rejected this pair and minted "Client Notification"
    # (cap_client_notification_046d13). Replaces the disputed BCP/DR pair in the gating set
    # per CHANGES.md row M3.
    "Inform Clients of Major ICT-Related Incidents and Mitigation Measures",
    "Incident Reporting",
    (
        "Ability to report significant operational or security incidents to the appropriate "
        "authority within required timeframes and formats."
    ),
    "obl_inform_clients_of_major_ict_related_incidents_and_mitigation_measures_56a123",
    "cap_incident_reporting_70cfe2",
    "reject",
)

_DISTINCT_INTERNAL_CONTROLS_CASE = (
    # CHANGES.md Appendix A4 (M2's over-generality guard, real-model tested): the pre-#187
    # curated baseline's "Internal Controls" catch-all, which #187 found bundling 6 unrelated
    # Arts 59-62 obligations onto it (issue-187 AC-BI-013). After #187 this Obligation was
    # accepted onto cap_network_security_compliance_f76b17 instead
    # (dora-reingest-187.jsonl:1935). The description literally "ensures compliance" -- the
    # exact catch-all the subsumption bullet's over-generality guard must stop from absorbing
    # a specific duty.
    "Comply with Network and Information Systems Security Requirements",
    "Internal Controls",
    (
        "Organizational control mechanisms that help ensure compliance, accuracy, and risk "
        "mitigation."
    ),
    "obl_comply_with_network_and_information_systems_security_requirements_cdf8e1",
    "cap_internal_controls_907019",
    "reject",
)

_GATING_CASES = [
    pytest.param(*_SUBSUMPTION_CASE, id="subsumption_eu_reporting_ict_incident"),
    pytest.param(*_QUALIFIER_CASE, id="qualifier_ict_operational_risk"),
    pytest.param(
        *_DISTINCT_CLIENT_NOTIFICATION_CASE,
        id="distinct_incident_reporting_vs_client_notification",
    ),
    pytest.param(
        *_DISTINCT_INTERNAL_CONTROLS_CASE,
        id="distinct_generic_internal_controls_vs_nis_security",
    ),
]


@pytest.mark.skipif(not _LLM_INTERFACE_MODEL, reason=_SKIPIF_REASON)
@pytest.mark.parametrize(
    (
        "obligation_text",
        "capability_name",
        "capability_description",
        "obligation_node_id",
        "capability_node_id",
        "expected_verdict",
    ),
    _GATING_CASES,
)
def test_reuse_verifier_on_real_dora_pairs_returns_expected_verdict(
    make_emitter: MakeEmitter,
    obligation_text: str,
    capability_name: str,
    capability_description: str,
    obligation_node_id: str,
    capability_node_id: str,
    expected_verdict: str,
) -> None:
    """#191 AC-BI-001/002/005: the recalibrated verifier prompt against real DORA pairs.

    Builds the exact messages `_verify_reuse` would build, calls `route_completion` for real
    against the configured LLM Provider, and parses the response via
    `parse_capability_reuse_verdict` -- the same two calls `derivation.py::_verify_reuse` makes
    in production, with no extra context added (PLAN.md §0.2: only the three tags reach the
    model). See the per-case comments above `_GATING_CASES` for the m1/M1 residual-risk notes
    (CHANGES.md Appendix A5/A6) and the G3/G4/G5a/A4 evidence citations.

    On a real GATING failure: STOP here, do not patch the prompt in this slice -- report back
    to the orchestrator and Critique with the verbatim verdict JSON below. LLM output is
    nondeterministic and this is a single sample, so report a flaky result as such.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    messages = _build_reuse_verification_messages(
        obligation_text=obligation_text,
        capability_name=capability_name,
        capability_description=capability_description,
    )
    emitter, _log_path = make_emitter(filename="reuse_verification_live.jsonl")
    result = route_completion(messages, model=model, call_completion=None, emitter=emitter)

    try:
        verdict = parse_capability_reuse_verdict(
            result.text, obligation_node_id, capability_node_id
        )
    except DomainMapperDerivationError as exc:
        pytest.fail(
            f"reuse-verification response was not parseable for obligation_node_id="
            f"{obligation_node_id!r}, capability_node_id={capability_node_id!r}: {exc}. Raw "
            f"completion text: {result.text!r}. Do not patch the prompt in this slice -- "
            "report back to the orchestrator and Critique with this verbatim output."
        )

    assert verdict.verdict == expected_verdict, (
        f"obligation_node_id={obligation_node_id!r}, capability_node_id="
        f"{capability_node_id!r}: expected verdict={expected_verdict!r}, got "
        f"{verdict.verdict!r}. Verbatim verdict JSON: {result.text!r}. Do not patch the prompt "
        "in this slice -- report back to the orchestrator and Critique with this verbatim "
        "output."
    )


# ---------------------------------------------------------------------------
# INFORMATIONAL (non-gating) case: CHANGES.md Appendix A5.

_BCP_DR_OBLIGATION_TEXT = "Establish and Maintain Business Continuity and Disaster Recovery Plans"
_BCP_DR_CAPABILITY_NAME = "Business Continuity Planning"
_BCP_DR_CAPABILITY_DESCRIPTION = (
    "Create, maintain, and update business continuity and contingency plans to ensure "
    "critical operations can continue during disruptions."
)
_BCP_DR_OBLIGATION_ID = (
    "obl_establish_and_maintain_business_continuity_and_disaster_recovery_plans_749d6c"
)
_BCP_DR_CAPABILITY_ID = "cap_business_continuity_planning_936d37"


@pytest.mark.skipif(not _LLM_INTERFACE_MODEL, reason=_SKIPIF_REASON)
def test_reuse_verifier_on_contested_bcp_dr_pair_reports_verdict(
    make_emitter: MakeEmitter,
) -> None:
    """#191 CHANGES.md Appendix A5: informational, non-gating real-model check.

    Ground truth disputed: ANALYSIS_FRAGMENTATION.md:53 says distinct; the faithful ablation
    judge says should_have_reused. Not gating (#191 FLAWS M3). This test only asserts that the
    response parses and that the verdict is one of the two valid values, then prints the
    verdict for the tracker (run with `-s` to see it). It is not graded against an expected
    outcome, and a reject or accept here is not escalated as a failure.
    """
    assert _LLM_INTERFACE_MODEL is not None  # narrows type; skipif already guards this
    model = _LLM_INTERFACE_MODEL

    messages = _build_reuse_verification_messages(
        obligation_text=_BCP_DR_OBLIGATION_TEXT,
        capability_name=_BCP_DR_CAPABILITY_NAME,
        capability_description=_BCP_DR_CAPABILITY_DESCRIPTION,
    )
    emitter, _log_path = make_emitter(filename="reuse_verification_live.jsonl")
    result = route_completion(messages, model=model, call_completion=None, emitter=emitter)

    try:
        verdict = parse_capability_reuse_verdict(
            result.text, _BCP_DR_OBLIGATION_ID, _BCP_DR_CAPABILITY_ID
        )
    except DomainMapperDerivationError as exc:
        pytest.fail(
            f"reuse-verification response was not parseable for the informational BCP/DR "
            f"pair: {exc}. Raw completion text: {result.text!r}."
        )

    assert verdict.verdict in {"accept", "reject"}
    print(f"INFORMATIONAL bcp_dr verdict={verdict.verdict!r}")  # noqa: T201
