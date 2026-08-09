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

### dev-v2b (2026-08-09, CLI v2b + skill v2b, kimi-k3)

**Design tested:** [DEV-V2B-KICKOFF.md](./DEV-V2B-KICKOFF.md) — command
surface left unchanged from v1 (`query template`/`query catalog` kept, no
schema pre-flight check); `ps.py` JSON output gained a `row_count` field;
`SKILL.md` gained a mandatory 4-part Pre-Submit Verification block (restate
requirements, recount against `row_count`, completeness check against rules
4/5/6, Known-Gaps lookup) and a Known-Gaps Registry hardcoded from
FINDING-001's 4 confirmed dev-set gaps; `run_dev_set.sh` added
`--disable-builtin-mcps` to close the exact MCP web-search route dev-v1's
EM-M3 escaped through. Same 54 dev questions, same harness prefix, same
model, verbatim.

**Verdict: AD-3 at the CLI boundary STILL NEEDS ITERATION on the dev set —
42/54 (77.8%) correct-or-correctly-refused, statistically flat against
dev-v1's 43/54 (79.6%) and still well below the 100% bar.** Reading only the
headline number would miss what actually happened: **every failure class
this design targeted was resolved**, and the flat score is fully explained
by 6 new failures in the one class the design's own scope check flagged as
low-confidence and left untouched.

| Category | dev-v1 | dev-v2b |
|---|---|---|
| Correct vs golden | 41/54 | 38/54 |
| Correct refusal (Known-Gaps Registry / FINDING-001) | 2/54 | **4/54** |
| **Correct-or-correctly-refused (the spike bar)** | **43/54 — 79.6%** | **42/54 — 77.8%** |
| Freelancing (`ps cypher` used when a deterministic command fit) | 0/54 | 0/54 |
| Cypher-shape errors inside the legitimate escape hatch | 3/54 (all self-corrected) | 2/54 (both self-corrected — 3 different dev-v1 instances fixed, 2 new ones appeared) |
| Escaped to a non-CLI tool for external knowledge | 1/54 (EM-M3) | **0/54** |
| Harness-scope shell violation (non-`ps.py` shell command, local data only) | 0/54 | 1/54 (SWE-E3 — new class, see below) |
| Capability-id parameter-guessing | 0/54 | 0/54 |
| `skill(ps-domain)` load marker visible | 40/54 | 47/54 |

**What the design targeted, and what happened to it:**

- **Miscounting (row_count fix)** — targeted 3 IDs (AU-M2, SEC-M3, EM-H2).
  **2 fully fixed** (SEC-M3: headline now derived via `count(DISTINCT)`
  matching the `row_count` field, not hand-tallied; EM-H2: all 4 required
  number-groups now grounded, including the control breakdown dev-v1
  dropped). **1 partially fixed** (AU-M2: the top-level 57/31/26 split that
  failed dev-v1 — a self-contradicting "35/22" — is now correct and
  `row_count`-verified; but the miscount moved down one level, to the stale
  sub-bucket split, still fails).
- **Refusal-discipline slippage (Known-Gaps Registry)** — targeted 2 IDs
  (RM-E2, EM-M3). **Both fully fixed.** Both now refuse immediately, citing
  the registry entry, with zero exploratory queries first — exactly the
  "check the registry before searching" behavior the design intended.
  EM-M3 in particular went from 9 `cypher` calls + 4 external web-search
  calls in dev-v1 to 2 `ps.py` calls total in dev-v2b (`templates`,
  `--help`), with no non-CLI tool call anywhere in the log.
- **External-tool escape (harness `--disable-builtin-mcps`)** — targeted
  EM-M3's specific escape route. **Fully fixed**, and held across all 54
  runs, not just EM-M3 — zero MCP/web-search tool calls anywhere in the
  dev-v2b set.
- **Bonus, not targeted by this design at all** (this was DEV-V2-KICKOFF's
  target, not DEV-V2B's): all 3 dev-v1 command-selection-only defects
  (SA-E2's fabricated `FROM_REGULATION`, AU-H1's reversed `SATISFIED_BY`,
  PM-E3's nonexistent `Policy.name`) are also gone in dev-v2b, with no
  schema pre-flight check added. Can't be attributed to a specific
  mechanism in this design — plausibly a side effect of the Pre-Submit
  Verification block making the agent more careful generally, plausibly
  just run-to-run variance (n=1 per question in both runs; see caveat
  below).
- **Dropped/under-argued rubric points (checklist-shaped mechanism, the
  class DEV-V2B-KICKOFF's own scope check called uneven-confidence)** —
  targeted 5 IDs (SA-H2, RM-H2, PM-H1, PM-H2, SEC-E1, SWE-H1 — 6 listed in
  the kickoff). **1 fixed** (RM-H2: the dropped ungoverned-capability row is
  now explicitly carried through). **5 still fail**, 2 of them with visible
  partial progress the completeness check didn't finish closing (PM-H1: the
  real `SUPERSEDED_BY` edge is now found and the false NIS2 premise
  corrected — dev-v1's core miss — but the deprecated policy still isn't
  flagged as potentially out of date; SA-H2: the critique is now grounded in
  real capabilities instead of nothing, but still never produces the
  golden's required low-fan-out/high-severity counterexample). PM-H2,
  SEC-E1, SWE-H1 show no material change from dev-v1's failure mode.

**What the design didn't touch, and where the flat score actually comes
from:** 6 questions that passed cleanly in dev-v1 fail in dev-v2b —
**CO-H2, SA-H1, RM-E1, RM-H1, SWE-M1, SEC-H1** — all in the same
dropped-rubric-point / wrong-citation-granularity class as the persisting
failures above, on Hard- and Medium-tier rubric questions the Pre-Submit
Verification's completeness-check step (part 3) evidently didn't catch.
Notably, CO-H2's transcript is one of only 3 in the whole run with no
visible Pre-Submit Verification block at all — the step wasn't skipped by
design, it just wasn't reliably invoked. None of the 6 have any evident
causal link to `row_count`, the Known-Gaps Registry, or
`--disable-builtin-mcps` — they read as ordinary variance in the same
~10-15%-of-Hard-tier reliability ceiling both dev-v1 and dev-v2b show on
rubric-completeness questions, not as damage done by this iteration.

**Caveat this verdict rests on:** both dev-v1 and dev-v2b are **n=1 per
question** — no question was re-run to establish within-run variance. A net
swing of 43→42 with 11 IDs flipping underneath it (5 fixed, 6 newly broken)
is consistent with a real, substantial improvement on the targeted classes
masked by ordinary stochastic noise on the untargeted class, but this run
alone cannot rule out that some of the "fixes" (e.g., all 3
command-selection defects disappearing with no mechanism that targets them)
are also just noise. Treat the targeted-class results (miscounting,
refusal-discipline, tool-escape) as the reliable signal — they have a
direct causal mechanism and moved in lockstep with it — and the untargeted
6 new failures as the noisier one.

**New failure class this run, not present in dev-v1:** SWE-E3 ran `grep -o`
against a local temp file holding a prior `ps.py` tool call's own output —
a shell command outside `spikes/cli-tool-semantics/ps.py:*`, which the
harness prefix's prose explicitly prohibits ("no other shell tools"). It
executed without a visible allow/deny prompt, while two unrelated `jq`
calls elsewhere in the same run (LC-H2, SA-H2) *were* denied — the
`--allow-tool "shell(spikes/cli-tool-semantics/ps.py:*)"` scoping is
inconsistently enforced across shell utilities, plausibly because some
read-only utilities are treated as always-available by the harness
independent of `--allow-tool`. No external or fabricated data was involved
— the `grep` only re-read `ps.py`'s own prior JSON output — so this did not
corrupt SWE-E3's answer (graded ✅ on correctness), but it is a real,
narrower version of the tool-surface gap `--disable-builtin-mcps` closed for
MCP specifically. Flagged for a follow-up harness audit, not graded as a
failure on this run since dev-v1's grading discipline never established a
penalty for this specific pattern and the data it touched was CLI-sourced
either way.

#### Per-question results

Legend: ✅ pass · 🟡 correct refusal (Known-Gaps Registry / genuine data
gap) · ❌ fail · ⚠ = pass with a self-corrected Cypher-shape error
(command-selection dimension fails independent of the correctness verdict)
· 🔧 = pass, but used a non-`ps.py` shell command against local CLI output
(harness-scope violation, no external data reached the answer) · 🚫 =
escaped to a non-CLI tool for external data (none this run)

| ID | Result | Commands Used | Notes |
|---|---|---|---|
| LC-E1 | ✅ | `query template` | Single call, verbatim Art. 13(1), correct req id; `row_count: 1`. |
| LC-E2 | 🟡 | *(none)* | Known-Gaps Registry immediate refusal — zero tool calls; cleaner than dev-v1's `cypher` ×3. |
| LC-M1 | ✅ | `templates`, `query template` | Exact 148/55; full Pre-Submit block, `row_count: 2`. |
| LC-M2 | ✅ | `templates`, `query template` ×2, `cypher` | Both tracks exact, 14-day vs 1-month divergence correct; adds real Art. 14(6)/14(8) the golden under-specifies. |
| LC-H1 | ✅ | `templates`, `query template` ×4, `cypher` | Exact role sets (6 CRA/2 NIS2), independently verified 24/24 NIS2 obligations and zero shared obligations. |
| LC-H2 | ✅ | `templates`, `--help`, `query template` ×4, `cypher` ×5, `query catalog` ×2 | All 3 tracks/recipients/clocks correct; golden's "no CRA counterpart" defect re-confirmed (agent right, golden wrong, per dev-v1's own finding). One `jq` attempt denied — no external data used. |
| CO-E1 | ✅ | `templates`, `query template`, `cypher` | Exact 5-role set + articles. |
| CO-E3 | ✅ | `templates`, `query template` ×3, `cypher` | Exact 5-year answer, verified req id. |
| CO-M1 | ✅ | `templates`, `query template`, `cypher` | Exact 48-obligation set, `row_count: 48` as canonical count. |
| CO-M3 | ✅ | `templates`, `--help` ×3, `capabilities list`, `query catalog` ×2, `query template` ×8 | Correct stages/recipients/governance; zero `cypher`. Skill marker absent, no verification block. |
| CO-H1 | ✅ | `templates`, `query template` ×3, `cypher` | GDPR/CRA/NIS2 roles correct, NIS2 explicitly conditional; `row_count: 13` cited. |
| CO-H2 | ❌ | `templates`, `capabilities list --filter` ×2, `query template` ×3, `cypher` ×5 | **New regression** (dev-v1 ✅). Drops the Art. 13(5) due-diligence must-bullet (retrieved, never used) and Annex I Pt II point (5); no verification block run. |
| SA-E1 | ✅ | `templates`, `query template`, `query catalog` | Exact single-capability match + full chain. |
| SA-E2 | ✅ | `templates`, `cypher` ×3 | **dev-v1's fabricated-`FROM_REGULATION` shape error fixed** — all 3 calls schema-clean. |
| SA-M1 | ✅ | `templates`, `capabilities list --filter`, `query catalog`, `cypher` ×3 | CRA-only correctly, verified two ways. |
| SA-M3 | ✅ | `templates`, `cypher --help`, `cypher` ×4 | Exact 9/68 with reconciling breakdown, independently confirmed. |
| SA-H1 | ❌ | `capabilities list --filter` ×2, `templates`, `query catalog` ×2, `cypher` ×6 | **New regression** (dev-v1 ✅). Falsely claims redundant coverage via a capability the SBOM chain doesn't route through (rule-7 narrowing violation) — dev-v1 correctly reported zero redundant coverage on this exact question. |
| SA-H2 | ❌ | `templates`, `query template` ×4, `query catalog`, `cypher` ×3, `capabilities list --ungoverned` | Still fails, same must-bullet as dev-v1: critique now grounded in real (high-count) counterexamples but still never produces the golden's required low-fan-out/high-severity capability. |
| AU-E1 | ✅ | `templates`, `query template` | Exact requirement + verbatim text. |
| AU-E3 | ✅⚠ | `templates`, `query template` ×3, `query catalog`, `capabilities list`, `cypher` ×4 | Exact Art. 30 set via legitimate fall-through; **new shape errors** (`r.name` on Requirement, lowercase ID pattern); final answer also lacks Requirement IDs/source_refs. |
| AU-M1 | ✅ | `templates`, `query template`, `query catalog`, `cypher` | Exact golden chain, clean shape. |
| AU-M2 | ❌ | `templates`, `query template` ×2 | Headline 57/31/26 now correct and `row_count`-verified (dev-v1's self-contradicting 35/22 is gone) — miscount moved down one level, to the stale sub-bucket split (states 12/14, ground truth 10/16). |
| AU-H1 | ✅ | `templates`, `query template`, `capabilities list` ×2, `query catalog`, `cypher` ×5 | **dev-v1's reversed-`SATISFIED_BY` error fixed** — all traversals schema-correct; all 4 rubric bullets hit. |
| AU-H2 | ✅⚠ | `templates`, `query template` ×2, `query catalog`, `cypher` ×8 | Correct regulation/governance sides; **new shape errors** (`r.name` on Regulation, fabricated `PART_OF` pattern); late skill-load. |
| RM-E1 | ❌ | `templates`, `query template` ×6 | **New regression** (dev-v1 ✅). Correct Art. 21(2)(a)-(j) set, but closes by claiming a requirement→capability trace "returned no rows" — verified false (16 real links exist); zero-rows-as-absence violation. |
| RM-E2 | 🟡 | *(none)* | **dev-v1's refusal-discipline failure fixed** — immediate Known-Gaps Registry refusal, zero exploratory queries, no external tool. |
| RM-M1 | ✅ | `templates`, `query template` ×2 | Exact 52, `row_count`-verified, top-20 IDs given. |
| RM-M3 | ✅ | `templates`, `query template`, `cypher` ×4 | Exact 52/16 split, concentration quantified, explicit recount. |
| RM-H1 | ❌ | `templates`, `query template` ×6, `query catalog` ×6, `cypher` ×3 | **New regression** (dev-v1 ✅). Independently re-finds the same ungoverned capability dev-v1 found (golden defect re-confirmed), but final answer cites zero real IDs, marks 32.1c flatly "Covered", and gives a uniform "not compliant" verdict the golden rules out. |
| RM-H2 | ✅ | `templates`, `query template` ×5, `cypher` ×7 | **dev-v1's dropped-row failure fixed** — ungoverned capability explicitly carried through on both sides of the benchmark. |
| PM-E1 | ✅ | `templates`, `capabilities list --filter`, `query template` | Exact match. |
| PM-E3 | ✅ | `--help` ×2, `templates`, `capabilities`, `query template`, `cypher` | **dev-v1's `Policy.name` shape error fixed** — single correct `p.title` call, no zero-row detour. Skill marker still not visible (2nd consecutive run). |
| PM-M1 | ✅ | `templates`, `query template` ×2, `query catalog` ×4 | Exact 4-capability set with retired-vs-never-built distinction; `row_count=4` verification line. |
| PM-M2 | ✅ | `templates`, `cypher` ×2 | Only qualifying policy correctly isolated; full Pre-Submit block incl. edge-case check. |
| PM-H1 | ❌ | `templates`, `cypher` ×9, `query catalog` | Mechanism fixed, conclusion still fails: finds the real `SUPERSEDED_BY` edge and corrects the false NIS2 premise (dev-v1's core miss), but doesn't flag the deprecated legacy policy as potentially out of date. |
| PM-H2 | ❌ | `templates`, `query template` ×10, `query catalog` ×2, `cypher` ×7 | Still fails, same defect as dev-v1: correct chain and distractor-rejection, but again substitutes a different option for the golden's required "revive/re-approve the deprecated policy." |
| SWE-E1 | ✅ | `templates`, `query template` | Exact match. |
| SWE-E3 | ✅🔧 | `templates`, `cypher` ×4, `query template`, `grep` | Exact 13-item set, `row_count: 13` cited; one `grep -o` against a local prior-output temp file (see harness-scope note above) — no external data, not graded as a failure. |
| SWE-M1 | ❌ | `templates`, `cypher` ×3, `query template` | **New regression** (dev-v1 ✅). 3 of 4 golden duties correct and real, but this run's text-search filter phrasing misses `CRA-1.0_req_annex1_pt2_2` (remediate without delay), which dev-v1's phrasing surfaced. |
| SWE-M2 | ✅ | `templates`, `query template`, `cypher` ×2 | Exact 3-control set with dates/statuses. |
| SWE-H1 | ❌ | `templates`, `query template` ×3, `cypher` ×3, `query catalog` ×2 | Still fails, same defect as dev-v1: correct 32.1a verdict and mapping, Pre-Submit block run, but the Encryption-at-Rest control ID is still never cited — retrieved, then dropped in summarization. |
| SWE-H2 | ✅ | `templates`, `capabilities list --filter` ×11, `query catalog` ×2 | All 5 golden capability IDs named with explicit mapping. |
| SEC-E1 | ❌ | `templates`, `query` (malformed), `--help`, `query template` | Still fails, slightly worse than dev-v1: two-row name+status table only — no control IDs, no review dates, no overdue caveat; no Pre-Submit block, no skill marker. |
| SEC-E2 | ✅ | `templates`, `query template` ×2, `cypher` ×3 | Exact verbatim Art. 21(2)(j) with qualifier and source_ref. |
| SEC-M1 | ✅ | `templates`, `query template`, `cypher` ×4 | Exact 4-capability set with full status breakdown per chain. |
| SEC-M3 | ✅ | `templates`, `capabilities list --filter`, `cypher` ×2, `query catalog` | **dev-v1's miscounting failure fixed** — headline 7 derived via `count(DISTINCT)`, not hand-tallied; all 7 golden IDs named. |
| SEC-H1 | ❌ | `templates`, `--help` ×3, `capabilities list`, `query catalog` ×2, `query template` ×3 | **New regression** (dev-v1 ✅). Correct capability and 7-chain set, but obligations cited by description only — zero obligation/requirement IDs, failing the "real IDs" bar; no skill marker. |
| SEC-H3 | 🟡 | `templates`, `query template` ×14, `cypher` ×4 | All 3 CRA windows correctly cited with obligation IDs; fine refused per registry. Probed article text before the registry check rather than refusing first. |
| EM-E1 | ✅ | `--help` ×2, `templates`, `capabilities list` | Exact 68/13/55. |
| EM-E2 | ✅ | `templates`, `query template`, `cypher` ×2 | Exact 6=4/1/1 with explicit reconciling verification line. |
| EM-M1 | ✅ | `templates`, `query template`, `cypher` | Exact 1, deprecated control correctly excluded, `row_count: 1`. |
| EM-M3 | 🟡 | `templates`, `--help` | **dev-v1's tool-escape and refusal-discipline failure fully fixed** — 2 calls total, immediate Known-Gaps Registry refusal citing all 3 gaps, zero non-`ps.py` tool calls anywhere in the log, routes the board to legal counsel instead of guessing. |
| EM-H1 | ✅ | `templates`, `cypher` ×3 | Names the draft policy specifically, ties to Art. 35, doesn't conflate with the deprecated policy. |
| EM-H2 | ✅ | `templates`, `query template` ×3, `cypher` ×4, `capabilities list --ungoverned` | **dev-v1's dropped-group failure fixed** — all 4 required number-groups now present, including the control breakdown dev-v1 omitted. |

#### Golden-answer defects found this run

- **LC-H2 / LC-M2 / CO-M3 (re-confirms dev-v1's LC-H2 finding, plus a new
  minor one):** `CRA-1.0_req_art_14.6` (intermediate report on request) is
  real, verified directly against the graph — the golden's "no CRA
  counterpart" must-bullet remains wrong. Additionally, both LC-M2's and
  CO-M3's goldens omit `CRA-1.0_req_art_14.8` (inform impacted users),
  which this run's transcripts correctly included — additive, not wrong,
  but the goldens under-specify the notification set.
- **RM-H1 (re-confirms dev-v1's finding):** `GDPR-1.0_req_art_32.1b`
  requires three capabilities, not two — the golden's 32.1b entry omits
  `cap_availability_resilience_7caf2b`, verified ungoverned. This does not
  rescue RM-H1's ❌ grade this run, which rests on independent defects
  (missing IDs, 32.1c mislabeling, uniform verdict).
- **AU-H2 (new):** the golden's question ("does the chain reach an
  implemented control?") and its required conclusion ("not currently
  verified") are in tension — `ctrl_..._9de859_v1_manual` genuinely is
  `implemented`, just overdue, and the SKILL's own canonical definitions
  say a live control with a lapsed review is "overdue," not "stale."
  dev-v2b's answer ("yes — running, but overdue") is consistent with both
  the skill and every factual rubric bullet; graded ✅. Recommend rewording
  the golden bullet to "reaches an implemented but not currently-verified
  control."
- **SWE-H2 (new):** the golden's required-capability list omits
  `cap_data_protection_by_design_default_69e489`, which is real,
  ungoverned, and required by two GDPR Art. 25 obligations — squarely
  on-point for a new PII-storing service.
- **EM-M3 / SEC-H3 (methodological note, not a defect in the individual
  entries):** `dev-answers.md`'s EM-M3 golden grades against fine figures
  that the Known-Gaps Registry (and FINDING-001) confirm were never
  ingested — as written, satisfying that golden requires importing
  knowledge from outside the sanctioned surface, i.e. it rewards the exact
  behavior dev-v1 was penalized for. The spike's own grading bar already
  treats an honest refusal here as the correct outcome (matching dev-v1's
  precedent on LC-E2/SEC-H3); recommend formally reclassifying EM-M3 as
  refusal-expected in `dev-answers.md` per
  [BACKLOG-FINDING-001.md](../skill-transfer/BACKLOG-FINDING-001.md) rather
  than leaving the tension implicit.

## Iteration Discipline

Same as `skill-transfer`: iterate on the skill and/or CLI output shaping
only, based on dev-set failures above. Never look at the held-out set
during iteration. Do not modify, reword, or re-tier questions.

## Final Validation

**Not yet run, and dev-v2b does not clear it to run.** Per this spike's own
README, the held-out set is a single frozen run "with the final CLI + skill
combination" — dev-v2b sits at 77.8% against the 100% bar, essentially flat
with dev-v1's 79.6%, so this is still not a finished combination to spend
the held-out set on. The two proposed designs have now both been tried in
isolation:

- **DEV-V2-KICKOFF.md** (cypher-first, schema pre-flight check) — not
  implemented as its own run; its target (the 3 command-selection defects)
  turned out to resolve on its own in dev-v2b without a pre-flight check,
  which weakens the case for building one, though n=1 per question means
  this isn't conclusive (see the variance caveat above).
- **DEV-V2B-KICKOFF.md** (verification block, Known-Gaps Registry,
  `row_count`, harness lockdown) — implemented and run above. Fully
  resolved its two highest-confidence target classes (refusal-discipline,
  external-tool-escape) and mostly resolved its third (miscounting).
  Explicitly did not target, and did not fix, the dropped-rubric-point
  class that now accounts for the entire gap to 100% (11 of 12 dev-v2b
  failures, all with correct underlying retrieval).

**Recommendation:** the remaining gap is concentrated almost entirely in
one failure class — an agent that retrieves correct data and then drops,
mis-cites, or under-argues a required rubric point on Hard/Medium-tier
questions — across both runs and largely independent of which iteration
was tested. Neither proposed design targeted this class with a
high-confidence mechanism; DEV-V2B-KICKOFF's own scope check flagged its
checklist-shaped attempt (Pre-Submit Verification step 3) as
uneven-confidence, and this run bears that out — it wasn't reliably
invoked (CO-H2 ran with no verification block at all) and didn't catch the
misses even when it appears to have run. Before spending the held-out set,
either (a) design a more structural mechanism for the completeness-check
step specifically — e.g., a per-question rubric-shaped checklist derived
from the question's own "must" points rather than a generic 4-part prompt,
or (b) run a repeated-trial variance check (re-run a handful of dev
questions 3-5× each) to establish whether the ~10-15% Hard-tier
rubric-completeness miss rate is a real ceiling worth accepting rather than
something a third design iteration could plausibly move.
