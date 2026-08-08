#!/bin/zsh
# © 2026 Cartman ApS. All rights reserved.
# Dev-set runner for the skill-transfer spike.
# Runs each question from docs/test-data/dev-questions.md as a fresh headless
# Copilot CLI session (kimi-k3), scoped to redis-cli only, saving one
# transcript per question. See RUNBOOK.md for the procedure this implements.

set -u
cd "$(dirname "$0")/../.."   # repo root — required so the CLI auto-loads .github/skills/ps-domain

OUT_DIR="spikes/skill-transfer/runs/dev-v1"
mkdir -p "$OUT_DIR"

HARNESS_PREFIX='You have access to a FalkorDB graph database. Query it ONLY by running a single plain command of the form: redis-cli GRAPH.QUERY policy_system "<cypher>" — no pipes, no chained commands, no other shell tools. If that command fails, report the error; do not fall back to reading files. For reference, today is 2026-08-01. Question:'

typeset -A QUESTIONS
QUESTIONS=(
  [LC-E1]="What's the text of CRA Article 13.1?"
  [LC-E2]="What's the worst fine we could face under GDPR for getting the basic processing rules wrong?"
  [LC-M1]="How many obligations does GDPR place on Data Processors vs. Data Controllers?"
  [LC-M2]="We have to report both actively exploited vulnerabilities and severe incidents under the CRA — are the deadlines the same for both?"
  [LC-H1]="Do CRA and NIS2 put duties on similar kinds of actors — is there something like a 'manufacturer' in both?"
  [LC-H2]="An actively exploited vulnerability in our product turns out to be both a severe incident under the CRA and a significant incident under NIS2 — walk me through every notification we owe, to whom, and by when."
  [CO-E1]="Who are the different regulated parties under GDPR?"
  [CO-E3]="Is there a minimum support period for products under the CRA, and how long is it?"
  [CO-M1]="What obligations does the Manufacturer role carry under CRA?"
  [CO-M3]="When we find out someone's actively exploiting a vulnerability in our product, what exactly do we have to report, to whom, and how fast?"
  [CO-H1]="We process customer data and we ship a software product — which of GDPR, CRA, and NIS2 actually apply to us, and as what kind of actor under each?"
  [CO-H2]="We found a vulnerability in an open-source component we bundle — is shipping our own fix enough, or does the CRA make us do more?"
  [SA-E1]="What capabilities does 'Maintain Security Logging' require?"
  [SA-E2]="Which of our capabilities does CRA's unauthorised-access protection duty land on?"
  [SA-M1]="Across CRA, NIS2, and GDPR — where do we need a security-logging-type capability?"
  [SA-M3]="How many of our 68 capabilities are actually covered by an approved policy, as opposed to a draft or deprecated one?"
  [SA-H1]="If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?"
  [SA-H2]="If a single capability of ours fails, which failure endangers the most obligations — and is that even the right way to think about criticality?"
  [AU-E1]="Which requirement does the 'Maintain Security Logging' obligation satisfy?"
  [AU-E3]="What does our record of processing activities have to contain under GDPR?"
  [AU-M1]="Trace the full path from CRA Art. 13.1 to whatever it ultimately requires us to be able to do."
  [AU-M2]="Show every path from a GDPR requirement down to a Control that verifies it."
  [AU-H1]="If an external auditor challenges our GDPR breach-notification compliance, what evidence trail do we have — and how much of it is actually current?"
  [AU-H2]="Trace the CRA's actively-exploited-vulnerability reporting duty from the regulation text all the way into our internal governance — does the trail reach a check that's actually running?"
  [RM-E1]="What security measures does NIS2 make essential and important entities implement, at minimum?"
  [RM-E2]="When is an incident 'significant' and therefore reportable under NIS2?"
  [RM-M1]="Which of our capabilities carry more than one regulatory duty?"
  [RM-M3]="How concentrated is our compliance risk — how much of what we have to do rides on a few shared capabilities versus many single-use ones?"
  [RM-H1]="Are we compliant with GDPR Article 32?"
  [RM-H2]="If we benchmark our NIS2 Article 21 readiness against our GDPR Article 32 posture, where do we stand?"
  [PM-E1]="What policy governs the 'Security Logging' capability?"
  [PM-E3]="What's the status and version of the Clinical Data Integrity Policy?"
  [PM-M1]="Which governed capabilities have zero implemented controls underneath, and why for each?"
  [PM-M2]="Which of our policies have all their supporting standards in a current — implemented or reviewed — state?"
  [PM-H1]="NIS2 was updated — which of our Policies are now potentially out of date?"
  [PM-H2]="GDPR's rule that staff may only process data on instructions routes through a deprecated policy — what are my options, and the risk of each?"
  [SWE-E1]="What's the implementation status of the Encryption-at-Rest control?"
  [SWE-E3]="What does the CRA require of the software I ship — the essential security properties?"
  [SWE-M1]="What does the CRA make me do about vulnerabilities in the third-party components I integrate?"
  [SWE-M2]="What checks run under the Data Protection & Security Policy, and what's the status and next review date of each?"
  [SWE-H1]="Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?"
  [SWE-H2]="I'm building a new microservice that stores customer PII in a database — what compliance-related capabilities should I be thinking about?"
  [SEC-E1]="Which checks exist under the Incident & Vulnerability Response Policy, and what state is each in?"
  [SEC-E2]="Does NIS2 explicitly require multi-factor authentication?"
  [SEC-M1]="Which capabilities have a policy on paper but no working check underneath?"
  [SEC-M3]="How many regulatory duties across CRA, NIS2, and GDPR land on our access-control/MFA capability — and which regulation actually says 'multi-factor authentication'?"
  [SEC-H1]="If an attacker exploited a missing MFA check today, which regulatory duties across CRA/NIS2/GDPR would we be in breach of?"
  [SEC-H3]="If we sit on a known actively-exploited vulnerability past the CRA's reporting windows, which duties have we breached, and what's the fine exposure?"
  [EM-E1]="How many capabilities do we track in total, and how many have a governing policy?"
  [EM-E2]="How many checks do we run, and what's the status breakdown?"
  [EM-M1]="How many Controls are currently overdue for review?"
  [EM-M3]="If the board asks 'what's the worst-case fine exposure across these three regulations?', what do I tell them?"
  [EM-H1]="Which of our draft policies are blocking GDPR readiness?"
  [EM-H2]="Give me a one-paragraph summary of our overall compliance posture I can bring to the board."
)

# Stable run order: sort IDs
ORDERED_IDS=(${(ko)QUESTIONS})

echo "Running ${#ORDERED_IDS} dev questions into $OUT_DIR"
for ID in "${ORDERED_IDS[@]}"; do
  LOG="$OUT_DIR/$ID.log"
  if [[ -s "$LOG" ]]; then
    echo "SKIP $ID (transcript exists)"
    continue
  fi
  echo "RUN  $ID ..."
  copilot -p "$HARNESS_PREFIX ${QUESTIONS[$ID]}" \
    --model kimi-k3 \
    --allow-tool "shell(redis-cli:*)" \
    > "$LOG" 2>&1
  echo "DONE $ID (exit $?)"
done
echo "All runs complete."
