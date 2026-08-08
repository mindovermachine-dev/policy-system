<!-- © 2026 Cartman ApS. All rights reserved. -->
# Kickoff Prompt: Held-Out (Blind) Validation Run

Paste the block below as the **first message of a fresh session**, with the
`ps-questions` repo added to the VS Code workspace alongside
`c4b-ps-internal`. It is written to stand alone — the new session has no
memory of how the dev set was run, so everything load-bearing is restated.

---

```
You are running the final, single-use held-out validation of the skill-transfer
spike. Work autonomously, but ask before any state-changing action beyond
running the batch (running the batch itself is pre-approved once you've
confirmed the setup below).

## Context

- Workspace: `c4b-ps-internal` (the policy-system repo) + `ps-questions`
  (the held-out question catalog — the blind half, frozen, deliberately kept
  out of the main repo until now).
- The skill under test: `.github/skills/ps-domain/SKILL.md` in
  `c4b-ps-internal`. It is FROZEN for this run — do not edit it. The whole
  point is an unbiased estimate of generalization with the final skill.
- FalkorDB runs in Podman (container `falkordb_test`, localhost:6379), graph
  `policy_system`. Access it ONLY via single plain commands:
  `redis-cli GRAPH.QUERY policy_system "<cypher>"` — no pipes, no chained
  commands, no other shell tools from the harness agent.
- Reference-date anchor: the answers are anchored to **2026-08-01**. Inject it
  into every prompt (see harness prefix below).

## Proven harness invocation (use exactly this; it was shaken down on the dev set)

Per question, one fresh headless Copilot CLI session, from the
`c4b-ps-internal` repo root so `.github/skills/ps-domain` auto-loads:

  copilot -p "$HARNESS_PREFIX Question: <verbatim question text>" \
    --model kimi-k3 \
    --allow-tool "shell(redis-cli:*)"

where HARNESS_PREFIX is (verbatim):

  You have access to a FalkorDB graph database. Query it ONLY by running a
  single plain command of the form: redis-cli GRAPH.QUERY policy_system
  "<cypher>" — no pipes, no chained commands, no other shell tools. If that
  command fails, report the error; do not fall back to reading files. For
  reference, today is 2026-08-01. Question:

## Procedure

1. Verify the environment: `podman ps` shows `falkordb_test`;
   `redis-cli GRAPH.QUERY policy_system "MATCH (n) RETURN count(n)"` answers.
2. Locate the held-out set in the `ps-questions` repo (blind questions +
   answers/grading criteria). Read it now — this is the single sanctioned look.
3. Write a runner script mirroring
   `c4b-ps-internal/spikes/skill-transfer/run_dev_set.sh`, output to
   `c4b-ps-internal/spikes/skill-transfer/runs/blind-v1/<ID>.log`. One fresh
   `copilot -p` invocation per question, verbatim question text only (no
   rephrasing, hints, or schema vocabulary).
4. Run the full set, then health-scan the transcripts: skill loaded
   (`skill(ps-domain)` present), zero `Search (grep)` file fallbacks, note any
   `Permission denied` (compound-command artifact, check whether the agent
   recovered).
5. Grade each answer against the held-out answers file: exact value / set /
   rubric per its stated grading. A refusal counts as success ONLY where the
   golden answer says the data is absent — OR where the golden answer asserts
   content that lives in the regulation files but was never extracted into the
   graph (the known FINDING-001 penalty-provision gap; such cases are
   "correct refusal," and note them explicitly as dataset-gap instances).
6. Record results in `c4b-ps-internal/spikes/skill-transfer/RUNBOOK.md`
   (results table) and update the Status line in
   `c4b-ps-internal/spikes/skill-transfer/README.md`.

## Success criteria (from the spike README)

- 100% of held-out questions answered correctly OR correctly refused for lack
  of information.
- Zero Cypher-shape errors (wrong property name, wrong ID pattern, reversed
  relationship) across all runs — even if the final answer is right.
- Every answer cites its provenance chain (Regulation → source_ref → ...).
- Honest refusal where data is missing (never fabricate).

## Discipline rules — non-negotiable

- The skill file is frozen. Do not edit it mid-run.
- One run only. No iterate-and-rerun on the blind set — it is the unbiased
  estimate, not a dev loop. If something fails, record it, don't fix-and-retry.
- Do not reword, re-tier, or re-grade questions to make results look better.
- Dev-set transcripts in `runs/dev-v1/` may be read for procedure reference
  only — NOT to shape how held-out questions are asked or graded.

## Deliverable

A verdict: **AD-6 holds** (skill generalizes to unseen questions) or **AD-6
needs revision**, with the results table and any new findings appended to the
runbook.
```

---

## Notes for the operator (not part of the prompt)

- **Frozen skill**: the run tests skill v1 as validated on the dev set. Any
  mid-run edit invalidates the comparison.
- **FINDING-001 expectation**: the held-out set was blind-generated from the
  same sources, so penalty/threshold-class questions may appear there too.
  They should resolve as correct refusals and are called out in the prompt so
  the grading session doesn't mis-score them as agent failures — but they
  must be *noted as dataset-gap instances*, not silently waved through.
- **One use**: the blind set's value is that it's seen once. Do not re-run
  after adjusting anything; a second run is no longer blind.
