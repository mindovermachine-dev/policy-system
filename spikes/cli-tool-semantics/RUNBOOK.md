<!-- © 2026 Cartman ApS. All rights reserved. -->
# Runbook: CLI Tool Semantics Spike Execution

**Companion to:** [README.md](./README.md) — the spike definition. This runbook
holds the harness-specific operating procedure and results, mirroring
`skill-transfer`'s [RUNBOOK.md](../skill-transfer/RUNBOOK.md) (same dev
question set, same grading discipline, different tool surface).

---

## Environment

- **FalkorDB**: same `policy_system` graph as `skill-transfer` and the query
  spikes — no reload step.
- **Access**: the `ps` CLI ([`ps.py`](./ps.py)) only. Shell permission scoped
  to `shell(spikes/cli-tool-semantics/ps.py:*)` — no raw `redis-cli`, no
  `python3 -c`, no other shell tools.

## The Agent's Tool Surface

```sh
spikes/cli-tool-semantics/ps.py <args>
```

See [README.md](./README.md#usage) for the command list. Command-selection
order is documented in `.github/skills/ps-domain/SKILL.md`, "CLI Command
Surface" section: `ps query template` → `ps query catalog` → `ps cypher`
(escape hatch, read-only, only when nothing deterministic fits).

## Harness Invocation

Headless Copilot CLI, one invocation per question, model `kimi-k3`, shell
permission scoped to `ps.py` only — see [run_dev_set.sh](./run_dev_set.sh)
for the exact harness prefix and question list. Run from the repo root so
the CLI auto-loads `.github/skills/ps-domain`.

## Skill Under Test

[../../.github/skills/ps-domain/SKILL.md](../../.github/skills/ps-domain/SKILL.md),
"CLI Command Surface" section + rule 9.

## Running the Development Set

Same 54-question dev set as `skill-transfer` (reused verbatim — see
[README.md](./README.md)'s status line). Reference date anchor
**2026-08-01**, baked into `run_dev_set.sh`'s harness prefix.

## Grading Bar

Same bar as `skill-transfer` ([RUNBOOK.md](../skill-transfer/RUNBOOK.md#grading-bar)),
plus this spike's own command-selection dimension:

- **100% of the set** answered correctly **or correctly refused due to lack
  of information**, graded against
  [dev-answers.md](../../docs/test-data/dev-answers.md).
- A refusal counts as success only where the golden answer says the data is
  absent; refusing an answerable question is a fail.
- "Partial" rubric scores count as fail.
- Every answer must cite its provenance chain.
- **No freelancing**: `ps cypher` used when `ps query template` or
  `ps query catalog` would have answered the question deterministically is a
  fail on this dimension, zero tolerance, regardless of final-answer
  correctness.
- **Cypher-shape discipline carries into the escape hatch**: a wrong
  property name, wrong ID pattern, or reversed relationship direction inside
  a legitimate `ps cypher` call is a fail on this dimension even if
  self-corrected before the final answer.
- **Parameter correctness**: `ps query catalog` capability ids must be
  resolved via `ps capabilities list`/`--filter` or an unambiguous
  name/id match, not guessed.

Grading was performed by three parallel grading passes (one per
audience-group batch), each cross-checking transcripts against
`dev-answers.md` independently; not re-graded by a fourth pass. Per
`skill-transfer`'s own caveat, treat this the same way — spot-check the
flagged Hard-tier failures before taking any grade as final.

## Results

### dev-v1 (2026-08-09, CLI v1 + skill rule 9, kimi-k3)

**Verdict: AD-3 at the CLI boundary NEEDS REVISION on the dev set — 43/54
(79.6%) correct-or-correctly-refused, below the 100% bar and a real
regression from `skill-transfer`'s 54/54 (100%) on the identical 54
questions via raw `redis-cli` Cypher.** The regression is not obviously the
CLI's command-routing fault: freelancing was zero, parameter-guessing was
zero, and the only command-selection defects were 3 self-corrected
Cypher-shape errors inside the legitimate escape hatch (out of 54 runs) plus
one run that abandoned the CLI/graph surface entirely for an external
web-search tool. The accuracy drop clusters downstream of retrieval — in
how agents summarized or reasoned over otherwise-correct CLI output — not
in which command they picked.

| Category | Result |
|---|---|
| Correct vs golden (value/set/rubric) | 41/54 |
| Correct refusal (golden asserted non-graph data, FINDING-001) | 2/54 (LC-E2, SEC-H3) |
| **Correct-or-correctly-refused (the spike bar)** | **43/54 — 79.6%** |
| Freelancing (`ps cypher` used when a deterministic command fit) | **0/54** |
| Cypher-shape errors inside the legitimate escape hatch | 3/54 (SA-E2, AU-H1, PM-E3 — all self-corrected before the final answer, still graded fail on this dimension per zero tolerance) |
| Escaped to a non-CLI tool instead of refusing | 1/54 (EM-M3 — external web search) |
| Capability-id parameter-guessing | **0/54** |
| `skill(ps-domain)` load marker visible in transcript | 40/54 confirmed; 14/54 marker not visible (see note below) |

**Note on the missing skill-load marker:** unlike `skill-transfer`'s
`redis-cli` harness, where `skill(ps-domain)` appeared in all 54 dev
transcripts, 14 of these 54 transcripts don't show the marker even though
their CLI behavior is otherwise fully skill-compliant (correct command
order, no freelancing). This reads as a logging/transcript-capture quirk of
this harness configuration rather than 14 real skill-load failures — flagged
for follow-up, not treated as a graded defect, except where it co-occurs
with an actual failure (PM-E3, which also has a Cypher-shape error).

**FINDING-001 status this run:** of the 4 questions `skill-transfer`
identified as genuine dataset gaps (LC-E2, RM-E2, EM-M3, SEC-H3 — penalty/
enforcement text never extracted into the graph), only 2 (LC-E2, SEC-H3)
were cleanly refused here. The other 2 were mishandled: RM-E2 answered with
the wrong regulatory focus instead of refusing, and EM-M3 called an
external web-search MCP tool to fetch real figures rather than refusing —
a more serious violation than `ps cypher` freelancing, since it abandons
the CLI/graph grounding this spike exists to test. This is a genuine
regression in refusal-discipline reliability versus `skill-transfer`'s dev
run (4/4 correct refusals there), not a re-confirmation of the same
behavior on a harder surface.

**Golden-answer defects found (not re-graded, recorded as pass — same
discipline as `skill-transfer`'s FINDING-003):**
- **LC-H2**: the transcript cites CRA Art. 14(6) (intermediate report on
  request) as a real CRA counterpart to NIS2's equivalent duty. Verified
  against `docs/regulations/CRA.md` L2955–2958 — it exists. The golden
  answer's must-bullet asserting "no CRA counterpart" is itself wrong.
- **RM-H1**: the agent independently found `cap_availability_resilience_7caf2b`
  (tied to GDPR Art. 32.1(b)) is ungoverned — a real gap the golden answer
  doesn't capture. Same class of defect as `skill-transfer`'s FINDING-003
  (SA-M4 under-counting).

#### Per-question results

Legend: ✅ pass · 🟡 correct refusal (FINDING-001) · ❌ fail · ⚠ = pass with a
self-corrected Cypher-shape error (command-selection dimension fails
independent of the correctness verdict) · 🚫 = escaped to a non-CLI tool

| ID | Result | Commands Used | Notes |
|---|---|---|---|
| LC-E1 | ✅ | `templates`, `query template` | Verbatim Art. 13.1 quote, correct req id. |
| LC-E2 | 🟡 | `query template`, `cypher` ×3 | Confirms FINDING-001: GDPR Art. 83 fines not ingested; exhaustive honest refusal. |
| LC-M1 | ✅ | `templates`, `query template` | Exact 148/55 controller/processor split. |
| LC-M2 | ✅ | `templates`, `query template`, `cypher` ×2 | Both tracks' deadlines and 14-day vs 1-month divergence exactly right; legitimate escape hatch for sub-clause anchoring. |
| LC-H1 | ✅ | `templates`, `query template` ×4 | Exact real role sets both regs; reasons by duty-theme similarity without overclaiming structural equivalence. |
| LC-H2 | ✅ | `templates`, `query template` (many), `capabilities list`, `query catalog` ×2 | All 3 notification tracks correct. Golden's "no CRA counterpart" must-bullet is itself wrong — CRA Art. 14(6) verified real; agent right, golden defective. |
| CO-E1 | ✅ | `templates`, `query template`, `cypher` | Exact 5-role set with per-role article citation; cypher used only to add provenance the template doesn't return. |
| CO-E3 | ✅ | `capabilities list`, `query template` (status, trace) | Exact 5-year quote via chain to support-period obligation. |
| CO-M1 | ✅ | `templates`, `query template` | Exact 48-obligation set, both golden endpoint ids present. |
| CO-M3 | ✅ | `templates`, `cypher` (S2 zero-rows, chain trace, catalog), `query catalog` ×2 | Correct deadlines/recipients/platform for the 3 CRA stages; legitimate escape hatch throughout. |
| CO-H1 | ✅ | `templates`, `query template` ×6, `cypher` (descriptions) | GDPR controller/processor, CRA manufacturer, NIS2 correctly left undetermined pending Annex I/II. |
| CO-H2 | ✅ | `templates`, `query template` (many), `capabilities list --filter`, `query catalog`, `cypher` ×2 | All 4 must-bullets hit: report+share fix, due diligence, Annex I points, conditional Art. 14 escalation. |
| SA-E1 | ✅ | `templates`, `query template` | Exact single-capability match. |
| SA-E2 | ✅⚠ | `templates`, `cypher` ×7, `query template` | Answer correct with full provenance, but fabricated a non-existent `FROM_REGULATION` relationship type twice before self-correcting via real vocabulary discovery. |
| SA-M1 | ✅ | `capabilities list --filter`, `query catalog`, `cypher` ×7 | Correctly CRA-only; explicit that NIS2/GDPR carry no logging-capability obligation. |
| SA-M3 | ✅ | `capabilities list --format json`, `cypher` ×5 | Exact 9-approved figure with fully consistent draft/deprecated/governed/uncovered breakdown; explicitly tried the deterministic command first. |
| SA-H1 | ✅ | `capabilities list --filter`, `query catalog` ×2, `cypher` ×6 | Correct capability id; correctly reports zero current NIS2/GDPR redundant coverage after checking all 44 NIS2 obligations. |
| SA-H2 | ❌ | `query template`, `cypher`, `query catalog --format json`, `cypher` ×2 | Correct capability (45 obligations) with full provenance and cross-checked count, but never grounds the "count ≠ criticality" critique with the golden's required concrete counterexample. |
| AU-E1 | ✅ | `templates`, `query template`, `cypher` | Exact requirement match; cypher used only to fetch `source_ref`. |
| AU-E3 | ✅ | `templates`, `query template` ×2, `cypher` ×2 | Template hit 0 rows on sub-clause anchoring, correctly fell through to cypher; exact Art. 30(1)(a–g)+30(2)-(4) set with provenance. |
| AU-M1 | ✅ | `templates`, `query template`, `cypher`, `query catalog` | Exact Req→Obligation→Capability chain; correctly flagged capability as ungoverned. |
| AU-M2 | ❌ | `templates`, `query template` (json) | All 57 chains enumerated correctly (31 current/26 not), but the agent's own summary sentence states a different, self-contradicting "35/22" split. |
| AU-H1 | ✅⚠ | `capabilities list` ×2, `query template` ×4, `cypher` ×3 | First hand-rolled chain query used a reversed `SATISFIED_BY` direction, self-corrected next call. Answer itself correct (6/8 covered-but-overdue). |
| AU-H2 | ✅ | `templates`, `query template` ×2, `cypher` ×5, `query catalog` | Exact overdue/planned conclusion matching rubric; catalog + narrowing cypher used correctly to isolate the CRA-only obligation. |
| RM-E1 | ✅ | `templates`, `query template` ×4, `cypher` ×2 | Exact Art. 21(2)(a–j) set with ids and source_refs. |
| RM-E2 | ❌ | `cypher` ×9, `capabilities list`, `query catalog` ×2, `query template` | Confirms the FINDING-001 gap exists (no Art. 23.3 node), but answered with the wrong regulatory focus instead of refusing — a refusal-discipline miss, not a search-effort miss. |
| RM-M1 | ✅ | `templates`, `query template` | Exact 52-capability set independently re-verified; top capability (45) matches golden. |
| RM-M3 | ✅ | `templates`, `query template` (json), `cypher` ×4 | Exact 52 shared/16 single-use split; cypher used only for concentration aggregates no template covers. |
| RM-H1 | ✅ | `templates`, `query template` ×3, `cypher` ×5 | Correctly diagnosed "zero rows ≠ absence"; independently found a real ungoverned capability the golden answer omits. |
| RM-H2 | ❌ | `templates`, `query template` ×4, `cypher` ~12 | Retrieved the ungoverned capability but the final coverage table still marks it flatly "Covered" with no caveat — a dropped row. |
| PM-E1 | ✅ | `query template` | Single-call exact match: policy id, title, status. |
| PM-E3 | ✅⚠ | `cypher` ×3 | First cypher used a non-existent `Policy.name` property, 0 rows, self-corrected via `keys(p)`. Final value (draft, v0.3) correct. `skill(ps-domain)` marker not visible — the one confirmed skill-load gap. |
| PM-M1 | ✅ | `query template --help`, `query template` | Exact 4-capability set with correct retired-vs-never-built distinction. |
| PM-M2 | ✅ | `query template`, `cypher` ×3 | Correctly identified no template fits this whole-graph aggregate; cross-checked with an inverse query. Exact match. |
| PM-H1 | ❌ | `templates`, `query template`, `cypher` ×6 | Never discovered the graph's one real `SUPERSEDED_BY` edge (checked only regulation-text supersession); missed the golden's required mechanism entirely. |
| PM-H2 | ❌ | `templates`, `query template` ×3, `query catalog` ×2, `cypher` ×6 | Correct real chain and correctly ruled out the processor-contract distractor, but substitutes a wrong option for the golden's required "revive deprecated policy" option and its risk. |
| SWE-E1 | ✅ | `templates`, `query template` | Exact match: implemented, next review 2026-08-15. |
| SWE-E3 | ✅ | `templates`, `cypher` ×3 | Exact 13-item list w/ real IDs; correctly flagged one golden point as genuinely absent from the graph. |
| SWE-M1 | ✅ | `capabilities list --filter`, `query catalog` ×3, `query template`, `cypher` | Golden's 4-item set plus 2 extra real, correctly-cited duties — additive, not wrong. |
| SWE-M2 | ✅ | `templates`, `query template` ×5, `cypher` | Exact 3-control set, all implemented, dates match golden exactly; cypher only for the one control the template couldn't match. |
| SWE-H1 | ❌ | `templates`, `query template` ×5, `capabilities list`, `query catalog` ×2 | Correct routing (discovery → catalog on two named capabilities), but never cites the actual Encryption-at-Rest control ID — generic answer where the rubric wants a real ID. |
| SWE-H2 | ✅ | `capabilities list`, `query catalog` ×5 | All 5 required capability IDs named; correctly separates "covered" vs. broken-chain gap vs. ungoverned. |
| SEC-E1 | ❌ | `templates`, `query template` ×2, `capabilities list` (unused) | Correct template match, but the answer table omits the golden's required overdue caveat on the first control. |
| SEC-E2 | ✅ | `templates`, `cypher` ×3 | Correct "Yes," Art. 21(2)(j) quoted verbatim with the "where appropriate" qualifier and provenance. |
| SEC-M1 | ✅ | `templates`, `query template`, `cypher` ×3 | Exact 4-capability set matching golden, including draft- and deprecated-chain caveats. |
| SEC-M3 | ❌ | `capabilities list --filter` ×2, `query catalog`, `cypher` | Enumerates all 7 correct golden IDs but headline miscounts them as "5 distinct obligations" — wrong answer to an exact-value "how many" question despite a correct underlying set. |
| SEC-H1 | ✅ | `capabilities list --filter` ×3, `query catalog` | Correct anchored routing; enumerates the full 7-obligation set and the current-evidence caveat (prose header miscounts as "six," but the graded set is right). |
| SEC-H3 | 🟡 | `templates`, `query template` ×2, `cypher` ×9, `query catalog` | Confirms FINDING-001: exhaustive full-text search before honestly refusing the fine figure. |
| EM-E1 | ✅ | `capabilities list --format json` | Exact match: 68 total, 13 governed, 55 ungoverned. |
| EM-E2 | ✅ | `templates`, `query template`, `cypher` ×2 | Exact match: 6 controls, 4/1/1 breakdown; textbook template-fallthrough-cross-check sequence. |
| EM-M1 | ✅ | `templates`, `query template` | Exact match: 1 overdue control, deprecated one correctly excluded. |
| EM-M3 | ❌🚫 | `cypher` ×9, `query template` ×3, **web search (MCP)** ×4 | After correctly exhausting the graph and confirming FINDING-001's gap, escalated to an unsanctioned external web-search tool instead of refusing — abandons the CLI/graph grounding this spike tests. Figures happen to be accurate but aren't graph-backed, and the answer self-contradicts its own "not graph-backed" caveat. |
| EM-H1 | ✅ | `cypher` ×2, `query template`, `cypher` | Names the one draft policy specifically; doesn't conflate it with the deprecated policy. |
| EM-H2 | ❌ | `templates`, `query template` ×6, `capabilities list --ungoverned`, `cypher` | Grounds 3 of 4 required number-groups but never states the required total-control count/breakdown. |

#### Failure pattern analysis

The 11 failures cluster into four classes, distinct from `skill-transfer`'s
own failure taxonomy (boundary/exclusion, blast-radius over-claiming,
granularity slips) — this run's failures sit mostly downstream of correct
retrieval, in how the agent summarized what it already had:

1. **Miscounting the agent's own correct data (3):** AU-M2, SEC-M3, EM-H2 —
   the agent retrieves or derives the right underlying set, then states a
   different (or incomplete) number in its final summary. This class did
   not appear at all in `skill-transfer`'s dev run. Candidate cause: the
   CLI's JSON/tabular output pushes counting into the agent's own
   arithmetic rather than a `count(*)` the raw-Cypher path could return
   directly — worth testing directly before assuming.
2. **Refusal-discipline slippage on known dataset gaps (2):** RM-E2,
   EM-M3 — both hit the same FINDING-001 gap that `skill-transfer`'s dev
   run refused cleanly on all 4 instances; this run only cleanly refused 2
   of 4. EM-M3's escalation to an external tool is the more serious of the
   two.
3. **Dropped or under-argued rubric points (5):** SA-H2, RM-H2, PM-H1,
   PM-H2, SEC-E1 — correct core facts, missing a required caveat, option,
   or counterexample. This is the same class `skill-transfer` also saw
   (its "boundary/exclusion discipline" cluster).
4. **Missing concrete ID citation (1):** SWE-H1 — answers in generalities
   where the rubric wants a specific, real ID.

No freelancing, no parameter-guessing, and no fabricated-graph-shape
answers reached the user uncorrected — the CLI's command-routing discipline
held. The accuracy gap versus `skill-transfer` is real but does not appear
to be a CLI-selection problem; it looks like a reasoning-over-retrieved-data
problem that the CLI's structured output may be making *more* visible
(explicit counts to get wrong) rather than causing outright.

## Iteration Discipline

Same as `skill-transfer`: iterate on the skill and/or CLI output shaping
only, based on dev-set failures above. Never look at the held-out set
during iteration. Do not modify, reword, or re-tier questions.

## Final Validation

Not yet run. Per this spike's own README, the held-out set is a single
frozen run "with the final CLI + skill combination" — since dev-v1 sits at
79.6% against the 100% bar, running held-out now would not be evaluating a
finished combination. Recommend an iteration pass first (see failure
pattern analysis above), then dev-v2, then held-out once dev clears the bar
— mirroring the discipline `skill-transfer` itself used.
