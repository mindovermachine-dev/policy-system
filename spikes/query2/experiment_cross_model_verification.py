#!/usr/bin/env python3
"""Standalone test of Candidate C (cross-model verification), per
q-approach4.md §7 fix 3: test before wiring into anything. Reuses the H11
trace from experiment_before_after.py's live run (qwen3-coder-next's answer
-- correct on citation, but silently omits the rubric-required "this is
hypothetical against today's real evidence, the capability is currently
governed by an approved Policy with an implemented Control" caveat).

Verifier is gemma4:12b -- a different model family from qwen3-coder-next
(Qwen vs. Gemma lineage), not a peer sample of the generator, per Candidate
C's own premise (q-approach4.md §10.6: self-preference bias needs a
different-family judge, not just a different checkpoint).

The verifier is asked one narrow, falsifiable question -- does this specific
trace support that specific completeness gap -- not an open "grade this
answer," per q-approach2.md's own finding that open-ended validator prompts
are where same-model validation already failed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query1"))

from openai import OpenAI  # noqa: E402

GENERATOR_ANSWER = """If an attacker exploited a missing MFA control today, the following regulatory obligations across CRA, NIS2, and GDPR would be breached:

### **CRA-1.0**
- **Requirement**: `CRA-1.0_req_annex1_pt1_2d`
  *Text*: "Products shall ensure protection from unauthorised access through appropriate control mechanisms, including authentication and identity or access management, and shall report on possible unauthorised access."
- **Obligation**: `obl_protect_against_unauthorised_access_ef908f`

### **NIS2-1.0**
- **Requirement 1**: `NIS2-1.0_req_art_21.2i`
  - **Obligations**: `obl_maintain_human_resources_security_access_control_and_asset_m_644c45`, `obl_maintain_human_resources_security_access_control_and_asset_m_40eba8`
- **Requirement 2**: `NIS2-1.0_req_art_21.2j`
  - **Obligations**: `obl_deploy_multi_factor_authentication_and_secured_communication_138a1f`, `obl_deploy_multi_factor_authentication_and_secured_communication_c2a8ea`

### **GDPR-1.0**
- **Requirement**: `GDPR-1.0_req_art_32.1b`
- **Obligations**: `obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_888591`, `obl_ensure_confidentiality_integrity_availability_and_resilience_of_p_408068`

A missing MFA control would directly violate NIS2's explicit multi-factor authentication requirement, and weaken compliance with the broader security and access control requirements under CRA and GDPR."""

GROUND_TRUTH_TRACE = """Tool call: run_cypher
MATCH (c:Capability {id:"cap_access_control_authentication_151816"})-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control)
RETURN p.id, p.status, ctrl.id, ctrl.implementation_status
Result: [["pol_data_protection_security_policy_8e4c18", "approved", "ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated", "implemented"]]"""

VERIFIER_PROMPT = f"""You will see a trace (a tool call and its real result from a compliance
knowledge graph) and an answer another model gave to a related question.

TRACE:
{GROUND_TRUTH_TRACE}

ANSWER:
{GENERATOR_ANSWER}

Narrow question: the trace shows the Capability in question ("Access Control & Authentication")
is CURRENTLY governed by an APPROVED Policy with an IMPLEMENTED Control -- i.e. the MFA control
described as "missing" in the question is not actually missing today; the scenario is
hypothetical. Does the ANSWER above explicitly state this -- that today's real evidence shows
this capability IS currently governed and implemented, so the violation described is
hypothetical rather than an existing gap?

Answer with exactly one word: YES (the answer states this) or NO (the answer does not state
this, even if implied)."""


def main() -> None:
    client = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
    verifier_model = "gemma4:12b"

    resp = client.chat.completions.create(
        model=verifier_model,
        messages=[{"role": "user", "content": VERIFIER_PROMPT}],
        temperature=0,
    )
    verdict = resp.choices[0].message.content.strip()

    print(f"Verifier model: {verifier_model} (different family from generator: qwen3-coder-next)")
    print(f"Verifier verdict: {verdict!r}")

    ground_truth = "NO"  # confirmed by direct inspection: the generator's answer never mentions
    # current governance/implementation status anywhere in its text.
    print(f"Ground truth (direct inspection of the generator's answer): {ground_truth}")

    caught = verdict.strip().upper().startswith("NO")
    print(f"\nReal catch: {caught}")
    if caught:
        print("Cross-model verification correctly flagged the same completeness gap this spike's "
              "H11 rubric already documents by hand -- real evidence for Candidate C's premise, "
              "on this one case.")
    else:
        print("Cross-model verifier did NOT catch it (or answered ambiguously) -- no evidence "
              "for Candidate C from this one case; per q-approach4.md §7 fix 3, this alone is not "
              "enough to wire it in.")


if __name__ == "__main__":
    main()
