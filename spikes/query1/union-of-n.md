# Union-of-N (from `q-approach2.md`'s "Further experiments")

Follow-up build from `q-approach2.md`'s combination experiments — of the four
tested there (self-consistency/union, temperature, generator+validator loop,
union+validator synthesis), plain union-of-N with mechanical (regex, not LLM)
id extraction was "the only one with unambiguous positive evidence." That
evidence lived only in `experiment_self_consistency.py`, a standalone script
never wired into `query_mechanism_v2.py` itself. Before adopting it, the
cheaper alternative from that same section (a deterministic citation-
completeness post-check) got its own full test first — see
`q-approach2.md`'s "Citation completeness as a deterministic post-check"
section: real but narrow value, not a substitute. This doc covers what came
next: wiring union-of-N in for real, then empirically re-verifying it the
same way `direction-correction.md` re-verified the direction corrector —
real questions, real models, diffed against the previously recorded
single-run verdict.

## Spec

**Problem**: after the grounding fix and the direction corrector closed two
Cypher-writing failure classes, what's left is "stopping early / under-
citing" — a model that retrieves the right data but doesn't put all of it in
its final answer, differently on different runs. `experiment_self_
consistency.py` showed this is genuinely stochastic (a 0/7, a 7/7, and a
non-convergent run on the same question), which rules out majority-vote —
the runs don't cluster near a right answer, they span the full range — but
means union across runs' cited ids reached 7/7.

**Approach**: sample the agent `UNION_RUNS_DEFAULT` (3) independent times per
question, then combine **mechanically, not with another LLM call**.
`q-approach2.md`'s "Further experiments" section found every tested form of
LLM-driven combination (a synthesis call, a validator pass, synthesis +
validator together) either lost information mechanically present in its own
context or failed to catch what was lost — so the design deliberately avoids
adding one here.

**Design decisions**:
- `extract_entity_ids` (id-shape regex, originally built for the citation-
  completeness experiment) scores each sampled run's own answer by how many
  distinct real-entity ids it cites — not judging any single run pass/fail
  (that role was tested and rejected in `q-approach2.md`'s citation-
  completeness section), just comparing runs against each other.
- The run whose own citations cover the most of the pooled (union) id set is
  kept **verbatim** as the answer. No re-synthesis — same "mechanical
  combination beats LLM judgment" finding driving this whole feature.
- Any ids the chosen run didn't cite but another sampled run did are
  appended as an explicitly flagged, unverified addendum — surfaced, not
  silently merged into prose, the same "show what changed" discipline the
  direction corrector's `direction_corrected` key and `run_cypher`'s error
  surfacing already follow in this codebase.
- `AgentTurnLimitExceeded` on an individual sampled run is treated as "this
  sample contributed nothing," not a hard failure — consistent with
  self-consistency's real data, where a non-convergent run among working
  ones was the normal case, not a rare edge case. Only when *every* sampled
  run fails to converge does that propagate (see "Result" below for why
  this matters in practice, not just as a design note).
- `v1`'s template path is completely unaffected — union sampling only
  applies once `QueryMechanismV2._ask_agent_union` is reached, i.e. only
  for the 13 questions approach 1 can't already answer for free.

## Build

`query_mechanism_v2.py`:
- `UNION_RUNS_DEFAULT = 3`, `extract_entity_ids` (moved here from
  `experiment_citation_completeness.py`, now shared rather than duplicated).
- `MechanismResult` gained `runs_sampled` and `union_ids_added`, so callers
  (and this doc's rerun script) can see what actually happened, not just the
  final text.
- `QueryMechanismV2.__init__` takes `union_runs: int = UNION_RUNS_DEFAULT`.
- `QueryMechanismV2._ask_agent_union` — the sampling/combination logic above.
  `ask()` now calls this instead of a single `_ask_agent` call once v1's
  `NoTemplateMatch` fires. `_ask_agent` itself (the single-run primitive) is
  unchanged.

`test_query_mechanism_v2.py`: the three pre-existing scripted plumbing tests
(tool-call round trip, error surfacing, turn-limit enforcement) are pinned to
`union_runs=1` — they test single-round-trip plumbing, orthogonal to
combination logic, and pinning keeps their scripted `FakeLLMClient` turn
counts exact. 8 new tests cover `extract_entity_ids` directly and the union
logic: best-coverage selection, mechanical gap-appending, a non-converging
sample being excluded without failing the question, and every sample failing
propagating correctly. **45/45 total** (was 35/35), v1 unaffected (39/39).

## Result: the tests prove the combination logic, not that it helps live

Same caveat `direction-correction.md` made about its own unit tests: proving
`_ask_agent_union`'s selection/append logic is correct against a scripted
`FakeLLMClient` is not the same claim as "this improves real model output."
That needed a real re-run, per `experiment_union_rerun.py`.

## Empirical re-verification

Re-ran H1, H13, H11 — the same three questions `experiment_citation_
completeness.py` most recently exercised live — through the actual wired
`QueryMechanismV2.ask()` (`union_runs` at its new default, 3), on both
`qwen3:14b` and, for H1/H11, `qwen3-coder-next:q4_K_M`. Diffed against the
specific single-run defects those two prior re-runs already documented.

**One clean, real win** — the core case this feature was built for.
H11/`qwen3-coder-next` previously cited 5 of 7 real obligations, silently
dropping the 2 NIS2 HR-security ones despite having retrieved them earlier
in its own trace. This re-run: the best of 3 sampled runs cited **all 7**,
correctly, and `union_ids_added` was empty — meaning this wasn't even a
patchwork rescue, one of the 3 independent tries simply got it fully right
on its own, and union's best-coverage selection correctly picked it. This is
exactly the effect `experiment_self_consistency.py`'s standalone test
predicted, now confirmed through the real wired mechanism on the real
question, not a synthetic stand-in. (Still incomplete against the full
rubric — it omits the golden answer's required "this capability is
currently governed by an approved Policy with an implemented Control, so
the question is hypothetical" caveat. That's a separate synthesis/framing
gap, not a citation-count gap, and was never something union-of-N was
expected to touch.)

**Two confirmed instances of the caveat already on record: union-of-N
doesn't fix a mistake every sample makes identically.** H1/`qwen3:14b`'s
chosen answer still never mentions the `GDPR-1.0_req_art_32.1` umbrella
clause — `golden-answers.md`'s own documented recurring miss — and none of
the 3 sampled runs' `union_ids_added` contents included it either; all three
independently missed the same clause. H11/`qwen3:14b` was worse: all 3 runs
answered "the graph does not track any regulatory obligations related to
missing MFA controls" — flatly wrong, 7 real obligations exist — with zero
ids cited across any of the 3 samples to combine. Both are consistent with
`q-approach2.md`'s own stated limit ("doesn't fix systematic bugs — if every
sample makes the same mistake, sampling more doesn't help"), now shown live
rather than asserted. H11/`qwen3:14b` also took 301s for zero benefit —
worth weighing against the cost this feature already concedes.

**One neutral result, correctly attributed rather than credited to the
fix.** H13/`qwen3:14b`'s chosen answer this run has fully correct numbers
(57 chains / 31 current, matching the graph exactly) — its previous single-
run defect (a hallucinated count) didn't recur. But `union_ids_added` was
empty and this question routes through `whole_graph_stats`, not
`run_cypher` rows a model cites by id — there was nothing for the union
mechanism to combine either way. This is ordinary run-to-run variance
landing on a good sample, the same phenomenon `direction-correction.md`'s
own re-run already documented and correctly declined to credit to a fix
that didn't touch the relevant code path.

**A new finding this re-run surfaced that no prior single-run test could:
sampling 3x can turn a working (if flawed) single run into total failure.**
H1/`qwen3-coder-next` previously converged reliably as a single run (with a
real defect — a false governance claim about a correctly-cited capability).
This re-run: **all 3 independent samples hit the 16-turn cap without ever
producing a final answer**, and `_ask_agent_union` correctly raised
`AgentTurnLimitExceeded` per its own design (every sample failed) — but the
practical effect is the mechanism went from "gives an answer with a known
flaw" to "gives nothing at all" on this exact (question, model) pair, purely
because it now asks for 3 independent successes at the same per-run turn
budget instead of 1. This is a real cost the "excludes non-converging runs,
only fails if all fail" design doesn't fully absorb: for a model already
close to its turn budget on a demanding multi-clause question, tripling the
number of independent attempts also triples exposure to that model's
per-run non-convergence rate, and this run shows that rate isn't
negligible for `qwen3-coder-next` on H1 specifically at 16 turns.

## What this establishes, and what it doesn't

Union-of-N's core promise — that at least one of several independent
samples often finds what a single run misses — is now confirmed on a real
question through the actual mechanism, not just the standalone experiment
that originally justified building it. It is not a general fix: two live
trials in this same re-run confirm it does nothing when a failure is
systematic rather than stochastic (both `qwen3:14b` cases here), matching
what was already on record as a known limit rather than contradicting it.
And it has a real, previously untested cost beyond raw latency: this re-run
is the first evidence that resampling can convert a single model's reliable
(if imperfect) convergence into an unlucky 0-for-3, trading a flawed answer
for no answer at all. **Open follow-up, not resolved here**: whether a
per-model or per-question turn-budget increase (already flagged as untested
in `q-approach2.md`'s "Next" item 4) would reduce this specific new risk, or
whether it's simply the honest price of this design for slower, more
turn-hungry models — worth checking before assuming either answer.
