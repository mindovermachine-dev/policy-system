# Golden Question Sets — Governance

This directory holds the question corpora the query mechanism is developed
and measured against. It exists to fix a specific, measured failure: the
§10 generalization stress test in [`../q-approach5.md`](../q-approach5.md)
showed the matcher was tuned against the same 39 questions it was then
graded on — and that the 20 "stress" questions used to detect that were
written by the same author, after the matchers existed. Neither set could
support a generalization claim. The discipline below is what makes a future
generalization claim actually mean something.

## The three sets

| Set | Dir | Purpose | Written | Frozen? |
|---|---|---|---|---|
| **dev** | [`dev/`](dev/) | Tune matchers, templates, regexes. Iterate freely. | Before/during building | No |
| **val** | [`val/`](val/) | Early-stopping / overfitting signal *during* dev. Optional at spike stage. | Held back from dev | Soft |
| **test** | [`test/`](test/) | The generalization claim. Run **once**, at a gate. | Cold — by someone who has not seen the matchers | **Hard-locked** |

The dev set is *expected* to be broken during development — that is its job.
The test set is the only one whose score supports a claim like "the
mechanism generalizes." Grading the dev set as if it were the test set is
the exact failure this layout exists to prevent.

## The one rule that matters: direction of travel

[`harness/score.py`](harness/score.py) may run a candidate mechanism and diff
its output against a golden answer.

**Nothing may run the mechanism to *produce* a golden answer.**

Golden answers come only from [`harness/compute_answer.py`](harness/compute_answer.py),
which computes them through an independent path — hand-written Cypher plus a
Python-side join, cross-checked against each other. This is not a
convention; it is the M7/H1 lesson enforced as structure. Both of those
golden answers were originally authored alongside `query_mechanism_v1.py` and
were *wrong* until an independent join caught the FalkorDB
projection-dependent row-dropping bug (see
[`../../query1/golden-answers.md`](../../query1/golden-answers.md) M7). When
the same hand writes the query and the expected answer, that class of bug is
invisible. For the **test** set especially, independent computation is
mandatory, not optional.

## Provenance is required, not decorative

Every question record carries provenance (see
[`questions.schema.json`](questions.schema.json)). The load-bearing field is
`tuned_against`:

- `tuned_against: true` → this question was visible while matchers were
  being written. A good score on it is **not** evidence of generalization.
- `tuned_against: false` **and** `written_before_matcher: true` and the
  author had no sight of the matchers → this is the only kind of question
  whose score supports a generalization claim.

The entire current dev corpus is `tuned_against: true`. That is an honest
statement of fact, not a defect — but it means none of it can anchor the
claim the test set exists to make.

## Authoring the test set

The test set is scaffolded but **deliberately empty**. Its questions must be
written *cold* — by the target users in
[`../../../readme.md`](../../../readme.md)'s audience table, or at minimum by
someone who has not read [`../clarifier.py`](../clarifier.py)'s regexes or the
catalog templates. If the person who built the matchers writes the test
questions, they are contaminated on arrival and the set is worthless.

When the test set is authored, record the freeze in
[`test/FREEZE.md`](test/FREEZE.md): who wrote it, when, and exactly which
matchers were live at freeze time (so a later change can't silently move the
goalposts).

## What this pass did NOT do

- **Existing answers were moved and provenance-tagged, not re-derived.**
  Recomputing all 59 golden answers through the independent harness is a
  separate, larger job. Questions whose answers were authored alongside the
  mechanism (notably **M7** and **H1**) are flagged
  `independent_recompute_needed: true` and must be recomputed before they
  can anchor any holdout claim.
- **No val set was populated.** Per decision, a spike at this stage uses
  dev + locked-test only. Add val if matcher iteration becomes multi-round
  and an in-dev overfitting signal is needed.
