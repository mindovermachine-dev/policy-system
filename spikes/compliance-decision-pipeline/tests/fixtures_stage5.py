# © 2026 Cartman ApS. All rights reserved.
"""The retroactive 162-instance pool for Stage 5's sampling dry-run
(PROGRESS.md Setup step 6 / README.md's "Dry-run Stage 5's sampling
strategy retroactively").

Question text is pulled verbatim from `docs/test-data/dev-questions.md`
(dev set, asked identically in both `cli-tool-semantics` runs) and
`spikes/skill-transfer/blind_questions.tsv` (held-out set, asked once via
raw Cypher). Pass/fail ground truth for each of the 3 runs (dev-v1, dev-v2b,
held-out) is pulled verbatim from the per-question result tables in
`spikes/cli-tool-semantics/RUNBOOK.md` and `spikes/skill-transfer/RUNBOOK.md`
-- not re-graded here, not invented. 54 questions x 2 CLI runs + 54 held-out
questions x 1 raw-Cypher run = 162 total instances, 33 of them known
failures -- both figures cross-checked against README.md's own tallies
("all 162 question-instances", "33 failure instances across the three
runs") and the per-run pass-count arithmetic in each RUNBOOK
(43/54, 42/54, 44/54 correct-or-correctly-refused => 11 + 12 + 10 = 33 fails).

Note: PROGRESS.md's "Next action" text calls this "the 108-transcript pool"
-- that figure only covers the two `cli-tool-semantics` runs (54x2) and
omits the 54 held-out instances also named in the same sentence. Recorded
here as a documentation discrepancy in the spike's own tracker, not
silently corrected -- this fixture uses the full, arithmetically-consistent
162, matching README.md's type-reliability table.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.stage5_sampling import Instance  # noqa: E402

# --------------------------------------------------------------------------
# Dev set question text (docs/test-data/dev-questions.md) -- identical
# questions asked in both cli-tool-semantics runs (dev-v1, dev-v2b).
# --------------------------------------------------------------------------

DEV_QUESTIONS = {
    "LC-E1": "What's the text of CRA Article 13.1?",
    "LC-E2": "What's the worst fine we could face under GDPR for getting the basic processing rules wrong?",
    "LC-M1": "How many obligations does GDPR place on Data Processors vs. Data Controllers?",
    "LC-M2": "We have to report both actively exploited vulnerabilities and severe incidents under the CRA — are the deadlines the same for both?",
    "LC-H1": "Do CRA and NIS2 put duties on similar kinds of actors — is there something like a 'manufacturer' in both?",
    "LC-H2": "An actively exploited vulnerability in our product turns out to be both a severe incident under the CRA and a significant incident under NIS2 — walk me through every notification we owe, to whom, and by when.",
    "CO-E1": "Who are the different regulated parties under GDPR?",
    "CO-E3": "Is there a minimum support period for products under the CRA, and how long is it?",
    "CO-M1": "What obligations does the Manufacturer role carry under CRA?",
    "CO-M3": "When we find out someone's actively exploiting a vulnerability in our product, what exactly do we have to report, to whom, and how fast?",
    "CO-H1": "We process customer data and we ship a software product — which of GDPR, CRA, and NIS2 actually apply to us, and as what kind of actor under each?",
    "CO-H2": "We found a vulnerability in an open-source component we bundle — is shipping our own fix enough, or does the CRA make us do more?",
    "SA-E1": "What capabilities does 'Maintain Security Logging' require?",
    "SA-E2": "Which of our capabilities does CRA's unauthorised-access protection duty land on?",
    "SA-M1": "Across CRA, NIS2, and GDPR — where do we need a security-logging-type capability?",
    "SA-M3": "How many of our 68 capabilities are actually covered by an approved policy, as opposed to a draft or deprecated one?",
    "SA-H1": "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?",
    "SA-H2": "If a single capability of ours fails, which failure endangers the most obligations — and is that even the right way to think about criticality?",
    "AU-E1": "Which requirement does the 'Maintain Security Logging' obligation satisfy?",
    "AU-E3": "What does our record of processing activities have to contain under GDPR?",
    "AU-M1": "Trace the full path from CRA Art. 13.1 to whatever it ultimately requires us to be able to do.",
    "AU-M2": "Show every path from a GDPR requirement down to a Control that verifies it.",
    "AU-H1": "If an external auditor challenges our GDPR breach-notification compliance, what evidence trail do we have — and how much of it is actually current?",
    "AU-H2": "Trace the CRA's actively-exploited-vulnerability reporting duty from the regulation text all the way into our internal governance — does the trail reach a check that's actually running?",
    "RM-E1": "What security measures does NIS2 make essential and important entities implement, at minimum?",
    "RM-E2": "When is an incident 'significant' and therefore reportable under NIS2?",
    "RM-M1": "Which of our capabilities carry more than one regulatory duty?",
    "RM-M3": "How concentrated is our compliance risk — how much of what we have to do rides on a few shared capabilities versus many single-use ones?",
    "RM-H1": "Are we compliant with GDPR Article 32?",
    "RM-H2": "If we benchmark our NIS2 Article 21 readiness against our GDPR Article 32 posture, where do we stand?",
    "PM-E1": "What policy governs the 'Security Logging' capability?",
    "PM-E3": "What's the status and version of the Clinical Data Integrity Policy?",
    "PM-M1": "Which governed capabilities have zero implemented controls underneath, and why for each?",
    "PM-M2": "Which of our policies have all their supporting standards in a current — implemented or reviewed — state?",
    "PM-H1": "NIS2 was updated — which of our Policies are now potentially out of date?",
    "PM-H2": "GDPR's rule that staff may only process data on instructions routes through a deprecated policy — what are my options, and the risk of each?",
    "SWE-E1": "What's the implementation status of the Encryption-at-Rest control?",
    "SWE-E3": "What does the CRA require of the software I ship — the essential security properties?",
    "SWE-M1": "What does the CRA make me do about vulnerabilities in the third-party components I integrate?",
    "SWE-M2": "What checks run under the Data Protection & Security Policy, and what's the status and next review date of each?",
    "SWE-H1": "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?",
    "SWE-H2": "I'm building a new microservice that stores customer PII in a database — what compliance-related capabilities should I be thinking about?",
    "SEC-E1": "Which checks exist under the Incident & Vulnerability Response Policy, and what state is each in?",
    "SEC-E2": "Does NIS2 explicitly require multi-factor authentication?",
    "SEC-M1": "Which capabilities have a policy on paper but no working check underneath?",
    "SEC-M3": "How many regulatory duties across CRA, NIS2, and GDPR land on our access-control/MFA capability — and which regulation actually says 'multi-factor authentication'?",
    "SEC-H1": "If an attacker exploited a missing MFA check today, which regulatory duties across CRA/NIS2/GDPR would we be in breach of?",
    "SEC-H3": "If we sit on a known actively-exploited vulnerability past the CRA's reporting windows, which duties have we breached, and what's the fine exposure?",
    "EM-E1": "How many capabilities do we track in total, and how many have a governing policy?",
    "EM-E2": "How many checks do we run, and what's the status breakdown?",
    "EM-M1": "How many Controls are currently overdue for review?",
    "EM-M3": "If the board asks 'what's the worst-case fine exposure across these three regulations?', what do I tell them?",
    "EM-H1": "Which of our draft policies are blocking GDPR readiness?",
    "EM-H2": "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.",
}

# --------------------------------------------------------------------------
# Held-out set question text (spikes/skill-transfer/blind_questions.tsv).
# --------------------------------------------------------------------------

BLIND_QUESTIONS = {
    "AU-E2": "Show me the audit evidence that our log retention control passed last quarter.",
    "AU-E4": "What must a data protection impact assessment contain, at minimum, under GDPR?",
    "AU-H3": "'Show me proof that you test whether your security measures actually work' — what can we point to, and where does the trail go cold?",
    "AU-H4": "If our log-retention check turns out to have failed, which regulatory requirements does that undermine?",
    "AU-M3": "Which of our automated controls are due for review in the next 30 days?",
    "AU-M4": "Which GDPR articles currently have only stale requirement-to-control evidence chains, and why?",
    "CO-E2": "When does CRA become effective, and what's its current status?",
    "CO-E4": "Once we've shipped a security update, how long does the CRA make us keep it available?",
    "CO-H3": "Put all three regulations' first-notification clocks side by side — if I set one internal escalation SLA, does 24 hours cover everything?",
    "CO-H4": "Which duties in these three regulations are purely procedural — the ones no technical security measure could ever satisfy?",
    "CO-M2": "Which of our extracted regulatory duties have the shakiest provenance confidence and should get a human review?",
    "CO-M4": "Where do NIS2's minimum security measures overlap with CRA's essential requirements and GDPR's security-of-processing rules?",
    "EM-E3": "How many of our GDPR evidence chains would currently hold up in an audit?",
    "EM-E4": "Which of our four policies are approved?",
    "EM-H3": "What should my team prioritize this quarter to move the needle on compliance?",
    "EM-H4": "How long, on average, does it take a Standard to go from draft to implemented in our organization?",
    "EM-M2": "What percentage of our Policies are still draft or deprecated rather than approved?",
    "EM-M4": "How much of our GDPR evidence problem is a governance problem versus an engineering problem?",
    "LC-E3": "What's the maximum fine under the CRA for not meeting its essential cybersecurity requirements?",
    "LC-E4": "Under GDPR, how quickly do we have to notify the supervisory authority of a personal data breach?",
    "LC-H3": "When does GDPR force us to appoint a Data Protection Officer, and does being classified as an essential entity under NIS2 change that analysis?",
    "LC-H4": "If we position our offering as free and open-source software, which CRA obligations do we still carry as an open-source software steward?",
    "LC-M3": "Which GDPR infringements fall into the higher fine tier versus the lower one?",
    "LC-M4": "Does the CRA apply all at once, or in phases — what are the exact dates?",
    "PM-E2": "List the standards under the Data Protection & Security Policy.",
    "PM-E4": "What capabilities does the deprecated Legacy Asset & Personnel Security Policy still govern?",
    "PM-H3": "Which policy state changes would unblock the most GDPR evidence chains?",
    "PM-H4": "If we simply delete the deprecated Legacy policy instead of replacing it, what happens to the things it governs and the GDPR evidence that routes through them?",
    "PM-M3": "GDPR requires records of processing and DPIAs — do our policies actually cover both duties?",
    "PM-M4": "What duties does NIS2 Article 20 create for our management body, and do any of our policies map to them?",
    "RM-E3": "How does the CRA define a 'severe' incident that manufacturers must report?",
    "RM-E4": "What's the maximum CRA fine for giving the authorities incorrect or misleading information?",
    "RM-H3": "A severe product incident hits at 09:00 on a Monday — walk the deadline timeline across all three regulations, and tell me where GDPR-only habits would make us miss one.",
    "RM-H4": "Which is the bigger exposure: the 55 things we're required to be able to do that no policy governs, or the one check that's overdue for review?",
    "RM-M2": "Which capabilities required by CRA have no governing policy?",
    "RM-M4": "Compare our two approved policies — what does each cover, and where are the soft spots?",
    "SA-E3": "Do NIS2 or GDPR need our SBOM capability for anything today?",
    "SA-E4": "Where does GDPR's encryption sub-clause land in our capability map?",
    "SA-H3": "Should we converge CRA's security logging, NIS2's unauthorised-access reporting, and GDPR's compliance monitoring onto a single shared capability — what would we gain, and what would we risk?",
    "SA-H4": "If we try to cover the CRA purely by mapping it onto our existing GDPR-driven capabilities, where does that strategy break down?",
    "SA-M2": "What capabilities does our internal Helvex SOP have in common with the CRA?",
    "SA-M4": "Of the things GDPR Article 32 expects us to be able to do, which lack an approved policy covering them?",
    "SEC-E3": "What's the status of the Automated Vulnerability Patch SLA Check?",
    "SEC-E4": "Does the CRA require products to detect and report unauthorised access?",
    "SEC-H2": "Across everything we verify, where are we most exposed — what would an auditor flag first?",
    "SEC-H4": "If the Encryption-at-Rest check fails its review on August 15, which regulatory duties does that put at risk?",
    "SEC-M2": "Which checks are overdue for review right now — not just due soon?",
    "SEC-M4": "Which checks come due for review before the end of August 2026, and which are already overdue?",
    "SWE-E2": "Which standards under the Data Protection & Security Policy are still in draft?",
    "SWE-E4": "When is the Access Control & MFA check next due for review?",
    "SWE-H3": "Our security scanner flagged missing rate-limiting on an endpoint that processes health data — does that block a GDPR-relevant control?",
    "SWE-H4": "Is my service, `checkout-api`, currently compliant?",
    "SWE-M3": "My service processes personal data — what does GDPR actually ask of me, technically?",
    "SWE-M4": "What does 'secure by default' mean under the CRA, and can I ever ship anything else?",
}

assert len(DEV_QUESTIONS) == 54, f"expected 54 dev questions, got {len(DEV_QUESTIONS)}"
assert len(BLIND_QUESTIONS) == 54, f"expected 54 held-out questions, got {len(BLIND_QUESTIONS)}"

# --------------------------------------------------------------------------
# Ground-truth failure IDs per run -- transcribed from the ❌ rows of each
# RUNBOOK's per-question results table. Counts cross-checked against each
# RUNBOOK's own headline arithmetic (54 - correct-or-correctly-refused).
# --------------------------------------------------------------------------

DEV_V1_FAILURES = {
    "SA-H2", "AU-M2", "RM-E2", "RM-H2", "PM-H1", "PM-H2",
    "SWE-H1", "SEC-E1", "SEC-M3", "EM-M3", "EM-H2",
}
assert len(DEV_V1_FAILURES) == 11, "cli-tool-semantics dev-v1: 54 - 43 = 11 failures"

DEV_V2B_FAILURES = {
    "CO-H2", "SA-H1", "SA-H2", "AU-M2", "RM-E1", "RM-H1",
    "PM-H1", "PM-H2", "SWE-M1", "SWE-H1", "SEC-E1", "SEC-H1",
}
assert len(DEV_V2B_FAILURES) == 12, "cli-tool-semantics dev-v2b: 54 - 42 = 12 failures"

HELD_OUT_FAILURES = {
    "CO-M2", "CO-M4", "AU-M4", "AU-H4", "PM-H3",
    "SEC-M2", "SEC-M4", "SEC-H4", "EM-E3", "EM-M4",
}
assert len(HELD_OUT_FAILURES) == 10, "skill-transfer held-out: 54 - 44 = 10 failures"


def build_pool() -> list:
    pool = []
    for qid, text in DEV_QUESTIONS.items():
        pool.append(Instance(question_id=qid, run="dev-v1", text=text, is_known_failure=qid in DEV_V1_FAILURES))
        pool.append(Instance(question_id=qid, run="dev-v2b", text=text, is_known_failure=qid in DEV_V2B_FAILURES))
    for qid, text in BLIND_QUESTIONS.items():
        pool.append(Instance(question_id=qid, run="held-out", text=text, is_known_failure=qid in HELD_OUT_FAILURES))
    return pool


PIPELINE_INSTANCE_POOL = build_pool()

assert len(PIPELINE_INSTANCE_POOL) == 162, f"expected 162 instances, got {len(PIPELINE_INSTANCE_POOL)}"
_n_known_failures = sum(1 for i in PIPELINE_INSTANCE_POOL if i.is_known_failure)
assert _n_known_failures == 33, f"expected 33 known failures, got {_n_known_failures}"
