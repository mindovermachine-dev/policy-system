"""Live-LLM evidence for the recalibrated capability-reuse verification prompt
(`CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT`).

Fake-caller tests in `test_derivation.py`/`test_prompts.py` only prove the recalibrated
guidance is the system prompt recorded at the LLM boundary; they script the verdict. Only a
real model call shows the recalibrated wording accepts the real DORA subsumption and
domain-qualifier cases while still rejecting genuinely distinct pairs. This is one targeted
pytest of verifier calls, not a further live DORA re-ingest.

`@pytest.mark.llm_live`, no `falkordb_live` marker: no graph is read or written here, only
`_build_reuse_verification_messages` -> `route_completion` -> `parse_capability_reuse_verdict`
against the real `derivation.py`/`prompts.py` functions.

Four GATING cases (parametrized):
  - `subsumption_eu_reporting_ict_incident` -> accept (data == test_derivation.py's
    constants `_EU_REPORTING_NAME`/`_EU_REPORTING_DESCRIPTION`/`_ICT_INCIDENT_OBLIGATION_TEXT`).
  - `qualifier_ict_operational_risk` -> accept (data == test_derivation.py's
    constants `_ICT_OP_RISK_NAME`/`_ICT_OP_RISK_DESCRIPTION`/`_OP_RISK_OBLIGATION_TEXT`).
  - `distinct_incident_reporting_vs_client_notification` -> reject.
  - `distinct_generic_internal_controls_vs_nis_security` -> reject (over-generality guard,
    real-model tested against the "Internal Controls" catch-all bundling).

One INFORMATIONAL (non-gating) case:
  `test_reuse_verifier_on_contested_bcp_dr_pair_reports_verdict` only checks the response
  parses and prints the verdict (run with `-s` to see it); its ground truth is disputed
  ("distinct" vs the faithful ablation judge's "should_have_reused"), so it is not asserted
  against an expected verdict.

Convention (mirrors `test_prompts_live.py`): on any GATING failure, do not patch the prompt
here -- report back with the verbatim verdict JSON. LLM output is nondeterministic and this is
a single sample per case, so a flaky result must be reported as such, not silently retried or
re-tuned away.
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
# test_derivation.py's module constants -- never paraphrased.

_SUBSUMPTION_CASE = (
    # Real DORA subsumption case.
    # Same data as test_derivation.py's constants `_ICT_INCIDENT_OBLIGATION_TEXT` /
    # `_EU_REPORTING_NAME` / `_EU_REPORTING_DESCRIPTION`.
    #
    # Known uncertainty: the description reaches 'EU authorities' while the duty targets
    # 'the Competent Authority' (usually national under DORA); a live reject citing
    # audience is an expected-risk outcome to report, not a prompt patch.
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
    # Real DORA domain-qualifier case. Same
    # data as test_derivation.py's constants `_OP_RISK_OBLIGATION_TEXT` /
    # `_ICT_OP_RISK_NAME` / `_ICT_OP_RISK_DESCRIPTION`.
    #
    # Residual risk: accept is the chosen design; an earlier ablation judge rated this pair
    # correctly_distinct. A live reject is a real failure to escalate, not to re-tune here.
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
    # Real DORA genuinely-distinct reject. The verifier correctly rejects this pair
    # and mints "Client Notification" (cap_client_notification_046d13). The disputed
    # BCP/DR pair is kept out of the gating set.
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
    # Over-generality guard, real-model tested: the curated baseline's "Internal Controls"
    # catch-all bundled 6 unrelated Arts 59-62 obligations onto it. This Obligation belongs
    # on cap_network_security_compliance_f76b17 instead. The description literally
    # "ensures compliance" -- the exact catch-all the subsumption bullet's over-generality
    # guard must stop from absorbing a specific duty.
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
    """The recalibrated verifier prompt against real DORA pairs.

    Builds the exact messages `_verify_reuse` would build, calls `route_completion` for real
    against the configured LLM Provider, and parses the response via
    `parse_capability_reuse_verdict` -- the same two calls `derivation.py::_verify_reuse` makes
    in production, with no extra context added (only the three tags reach the
    model). See the per-case comments above `_GATING_CASES` for the residual-risk notes.

    On a real GATING failure: STOP here, do not patch the prompt -- report back
    with the verbatim verdict JSON below. LLM output is
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
# INFORMATIONAL (non-gating) case.

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
    """Informational, non-gating real-model check.

    Ground truth disputed: ANALYSIS_FRAGMENTATION.md:53 says distinct; the faithful ablation
    judge says should_have_reused. Not gating. This test only asserts that the
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
