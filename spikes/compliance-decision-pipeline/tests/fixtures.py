# © 2026 Cartman ApS. All rights reserved.
"""Known target cases, question text pulled verbatim from
`docs/test-data/dev-questions.md` (dev set) and
`spikes/skill-transfer/blind_questions.tsv` (held-out set). See
PROGRESS.md's "Target cases" section for the full grounding on why each
one is here and what mechanism it validates -- this file is only the
machine-checkable expectations, not the rationale.

Do not add a case here without a corresponding row in PROGRESS.md.
"""

from __future__ import annotations

REFERENCE_DATE = "2026-08-01"

# --------------------------------------------------------------------------
# Ground-truth IDs pulled directly from the live graph this session (see
# PROGRESS.md's Stage 4 sections) -- not invented. Shared by
# tests/test_stage4.py and pipeline/run_target_cases.py so both read from
# one place rather than duplicating the same hardcoded IDs.
# --------------------------------------------------------------------------

INCIDENT_POLICY_CONTROLS = {
    "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual",
    "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v2_automated",
}
DEPRECATED_PAST_DUE_CONTROL = "ctrl_std_pol_legacy_asset_personnel_security_policy_7ed6c2_v1_manual"
TRUE_OVERDUE_CONTROL = "ctrl_std_pol_incident_vulnerability_response_policy_9de859_v1_manual"

MFA_OBLIGATIONS = {
    "obl_protect_against_unauthorised_access_ef908f",
    "obl_maintain_human_resources_security_access_control_and_asset_m_644c45",
    "obl_maintain_human_resources_security_access_control_and_asset_m_40eba8",
    "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f",
    "obl_deploy_multi_factor_authentication_and_secured_communication_c2a8ea",
    "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591",
    "obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_408068",
}

SECURITY_LOGGING_CAPABILITY = "cap_security_logging_c4d9e2"

# --------------------------------------------------------------------------
# SA-H1 -- capability's real regulation-routing (verified live: only CRA
# requires the SBOM capability today, zero NIS2/GDPR redundant coverage).
# Reuses check_regulation_scope, same mechanism as AU-H4, different capability.
# --------------------------------------------------------------------------

SBOM_CAPABILITY = "cap_component_inventory_sbom_management_b5223c"

# --------------------------------------------------------------------------
# SA-H2 -- the actual maximum-fanout capability across the whole catalog
# (verified live: 45 obligations require it, the next-highest is 30).
# Targets check_fanout_maximum.
# --------------------------------------------------------------------------

MAX_FANOUT_CAPABILITY = "cap_data_subject_rights_fulfilment_communication_8eedf0"
MAX_FANOUT_COUNT = 45

# --------------------------------------------------------------------------
# CO-H2 -- the CRA obligations a correct "beyond shipping our own fix"
# answer must cite (Art 13(5)-(6) + Annex I Pt II points 1/2/4/5 + the
# conditional Art 14 escalation), pulled live via the Requirement->
# SATISFIED_BY->Obligation edge -- not invented, see dev-answers.md L315-341.
# Targets check_existence.
# --------------------------------------------------------------------------

CO_H2_REQUIREMENT_IDS = [
    "CRA-1.0_req_art_13.5",
    "CRA-1.0_req_art_13.6",
    "CRA-1.0_req_annex1_pt2_1",
    "CRA-1.0_req_annex1_pt2_2",
    "CRA-1.0_req_annex1_pt2_4",
    "CRA-1.0_req_art_13.8c",  # CVD policy -- graph maps this to 13.8c, not annex1_pt2_5 as dev-answers.md's provenance states; a known golden/graph citation mismatch, not a bug here
    "CRA-1.0_req_art_14.1",
]
CO_H2_OBLIGATIONS = {
    "obl_exercise_due_diligence_on_third_party_and_open_source_components_e505f1",
    "obl_report_and_remediate_vulnerabilities_in_integrated_components_0cd616",
    "obl_identify_and_document_components_via_software_bill_of_materials_dcfaae",
    "obl_remediate_vulnerabilities_without_delay_via_security_updates_248f5a",
    "obl_publicly_disclose_fixed_vulnerability_information_d83c6b",
    "obl_maintain_a_coordinated_vulnerability_disclosure_policy_c182fe",
    "obl_report_actively_exploited_vulnerabilities_8fd384",
}

# --------------------------------------------------------------------------
# SEC-H4 -- the Encryption-at-Rest control's own capability and its real
# obligation set (verified live), plus two obligations that genuinely
# require *other* capabilities under the same shared Standard (MFA ->
# access-control, logging -> security-logging) -- the RUNBOOK-recorded
# over-claim ("listed duties verified by the v2/v3 controls, not failing
# on Aug 15"). Golden text for SEC-H4 itself is not in this repo (held-out
# question) -- this fixture is RUNBOOK-note-validated, not golden-validated,
# per PROGRESS.md's discipline for this case. Targets check_existence,
# scoped to the failing control's own capability.
# --------------------------------------------------------------------------

DATA_ENCRYPTION_CAPABILITY = "cap_data_encryption_0e50d3"
DATA_ENCRYPTION_OBLIGATIONS = {
    "obl_protect_data_confidentiality_through_encryption_d77019",
    "obl_maintain_cryptography_and_encryption_policies_40a163",
    "obl_maintain_cryptography_and_encryption_policies_640faf",
    "obl_apply_pseudonymisation_and_encryption_as_controller_fc1f7e",  # GDPR Art 32(1)(a) -- golden's "primary casualty"
    "obl_apply_pseudonymisation_and_encryption_as_processor_92cf59",
}
# Verified by v2 (Access Control & MFA Enforcement Audit), not v1 -- over-claimed if cited as at risk from v1 failing.
SEC_H4_OVERCLAIMED_MFA_OBLIGATION = "obl_deploy_multi_factor_authentication_and_secured_communication_138a1f"
# Verified by v3 (Log Retention & SIEM Ingestion Integrity Check), not v1 -- same over-claim shape.
SEC_H4_OVERCLAIMED_LOGGING_OBLIGATION = "obl_maintain_security_logging_c427be"

# --------------------------------------------------------------------------
# EM-M4 -- root-cause classification (governance vs. engineering) for the
# two capabilities RUNBOOK.md's own failure note names by structure, not
# by golden text (held-out question): "Clinical-draft" is
# cap_data_protection_impact_assessment_a51acb, governed by the (draft-
# status) Clinical Data Integrity policy with zero Controls at all --
# nothing built, a pure governance gap. "incident-v2" is the three
# capabilities under the (approved) Incident/Vulnerability Response policy,
# which already has a live v1 Control (chain not stale/broken) but also a
# still-`planned` v2 Control -- an in-progress build, an engineering gap,
# not a governance one. Verified live this session: GDPR obligations
# requiring the Clinical capability = 10; summed across the three Incident
# capabilities = 10 -- reproducing RUNBOOK's own "Clinical-draft 10 vs
# incident-v2 10" note exactly, from independent re-derivation, not by
# construction. Targets check_evidence_gap_root_cause.
# --------------------------------------------------------------------------

EM_M4_CLINICAL_CAPABILITY = "cap_data_protection_impact_assessment_a51acb"
EM_M4_INCIDENT_CAPABILITIES = {
    "cap_security_incident_reporting_449fa4",
    "cap_incident_handling_4cf73e",
    "cap_business_continuity_disaster_recovery_9c1c32",
}
# A resolved capability under the same, otherwise-implemented security
# policy -- the must-not-flag non-regression case (neither governance nor
# engineering gap; the chain is fully live, nothing planned or missing).
EM_M4_RESOLVED_CAPABILITY = "cap_data_encryption_0e50d3"
# The legacy policy is deprecated -- excluded from the evidence problem
# entirely (deprecated Controls left the review cycle, they didn't fail
# it -- same exclusion discipline as SEC-M2/SEC-M4's overdue rule check).
EM_M4_EXCLUDED_CAPABILITY = "cap_asset_personnel_security_management_e68e9a"

# --------------------------------------------------------------------------
# Stage 1 -- alias table target cases
# --------------------------------------------------------------------------

STAGE1_CASES = [
    {
        "id": "AU-M4",
        "set": "blind",
        "question": (
            "Which GDPR articles currently have only stale "
            "requirement-to-control evidence chains, and why?"
        ),
        "must_flag_terms": {"stale"},
        "must_not_flag_terms": {"overdue", "deprecated"},
    },
    {
        "id": "AU-H2",
        "set": "dev",
        "question": (
            "Trace the CRA's actively-exploited-vulnerability reporting "
            "duty from the regulation text all the way into our internal "
            "governance — does the trail reach a check that's actually "
            "running?"
        ),
        "must_flag_terms": set(),
        "must_not_flag_terms": {"stale", "overdue", "deprecated"},
    },
    {
        "id": "SEC-M2",
        "set": "blind",
        "question": "Which checks are overdue for review right now — not just due soon?",
        "must_flag_terms": {"overdue"},
        "must_not_flag_terms": {"stale", "deprecated"},
    },
    {
        "id": "SEC-M4",
        "set": "blind",
        "question": (
            "Which checks come due for review before the end of August "
            "2026, and which are already overdue?"
        ),
        "must_flag_terms": {"overdue"},
        "must_not_flag_terms": {"stale", "deprecated"},
    },
]

# entity-type extraction target cases (Stage 1 capture, Stage 4 consumes)
STAGE1_ENTITY_TYPE_CASES = [
    {
        "id": "EM-E3",
        "set": "blind",
        "question": "How many of our GDPR evidence chains would currently hold up in an audit?",
        "expected_entity_type": "chain",
    },
]

# --------------------------------------------------------------------------
# Stage 4 -- rule check target cases (SEC-M2/SEC-M4: overdue must exclude
# deprecated, exact-set, not exclude-with-caveat)
# --------------------------------------------------------------------------

RULE_CHECK_CASES = [
    {
        "id": "SEC-M2",
        "question": "Which checks are overdue for review right now — not just due soon?",
        # The failing transcript's answer included a deprecated-status
        # Control in the "overdue" set (with a caveat noting it was
        # deprecated/moot) -- golden requires exclusion, not
        # exclusion-with-caveat. Simulated here as the answer under test.
        "answer_overdue_control_ids": None,  # filled in at runtime from a live query + a deliberately-injected deprecated control
        "must_flag": True,
    },
    {
        "id": "SEC-M4",
        "question": (
            "Which checks come due for review before the end of August "
            "2026, and which are already overdue?"
        ),
        "answer_overdue_control_ids": None,
        "must_flag": True,
    },
]

# --------------------------------------------------------------------------
# Stage 4 -- scope-match target cases (AU-H4/SEC-H4: over-claiming beyond
# the chain that actually routes through the named node)
# --------------------------------------------------------------------------

SCOPE_MATCH_CASES = [
    {
        "id": "AU-H4",
        "set": "blind",
        "question": (
            "If our log-retention check turns out to have failed, which "
            "regulatory requirements does that undermine?"
        ),
        # golden: CRA-only (+ Helvex SOP) undermined; NOT NIS2/GDPR via
        # this capability. The failing answer claimed NIS2/GDPR "weakened"
        # too, via the shared standard rather than the routed chain.
        "over_claimed_regulations": {"NIS2", "GDPR"},
        "must_flag": True,
    },
    {
        "id": "SEC-H4",
        "set": "blind",
        "question": (
            "If the Encryption-at-Rest check fails its review on August "
            "15, which regulatory duties does that put at risk?"
        ),
        # golden: isolate Art. 32(1)(a) as primary casualty, CRA hedged.
        # Failing answer listed duties verified by other (non-failing)
        # controls -- over-broad blast radius.
        "must_flag": True,
    },
]

# --------------------------------------------------------------------------
# Stage 4 -- existence-grounding target cases (5 ambiguous dev-v2b cases:
# correct retrieval, incomplete/mis-cited final answer)
# --------------------------------------------------------------------------

EXISTENCE_GROUNDING_CASES = ["SA-H1", "SA-H2", "SEC-E1", "SEC-H1", "CO-H2"]
