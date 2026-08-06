# Deterministic Relationship-Direction Correction (from Approach 3)

Follow-up build from `q-approach3.md`'s first reflection — "adopt, don't
re-research" — written up separately per that doc's own instruction not to
retrofit a build into a reflections-only document.

## Spec

**Problem**: `query_mechanism_v2.py`'s agent, writing freehand Cypher against
`GRAPH_SCHEMA`, twice reversed a relationship direction (H1's
`(pol)<-[:SUPPORTED_BY]-(:Standard)` instead of the schema's
`Policy-[:SUPPORTED_BY]->Standard`; H11's
`(reg:Regulation)<-[:DEFINES]-(role:Role)` instead of
`Regulation-[:DEFINES]->Role` — see `q-approach2.md`'s "Result" table). Both
failures were silent: a reversed edge doesn't error in Cypher, it just
matches zero rows, indistinguishable from "this doesn't exist." H1's reversal
alone hid three real `implemented` Controls and flipped the agent's final
answer from "partial" to an overly pessimistic "non-compliant."

**Approach, per `q-approach3.md`'s research**: not another prompting fix —
those reduce the *rate* of this mistake, they don't eliminate it. Instead, a
deterministic parse-and-correct step run on every `run_cypher` call before it
reaches FalkorDB, the same category of fix the winning
[Cypher Direction Competition](https://github.com/tomasonjo/cypher-direction-competition)
solution and LangChain's `CypherQueryCorrector` represent, and the same
"decision moved out of prose and into deterministic Python" instinct
`_annotate_trust()` already represents elsewhere in this codebase (see
`q-approach1.md`).

**Design decisions**:
- `SCHEMA_RELATIONSHIP_DIRECTIONS` is the structured source of truth (a
  `dict[str, tuple[from_label, to_label]]`) — every relationship type in this
  graph has exactly one valid direction, so the type name alone determines
  the correct direction once both endpoint labels are known. Kept in sync
  with `GRAPH_SCHEMA`'s prose (what the model reads) by a test that checks
  every canonical pair appears in the prose text.
- Correction only fires when **both** endpoints of a hop resolve to a label
  that fits one of the two orientations of the schema's pair for that
  relationship type — inline (`(:Standard)`) or via an earlier binding of the
  same variable anywhere else in the query text (`(pol:Policy ...)` earlier,
  `(pol)` later). No label match on either side → left untouched. This is
  deliberately conservative: it corrects what it can verify against the
  query's own labels, not what it infers from write order or variable-name
  guessing.
- A query that legitimately traverses backward (e.g. Control back to
  Standard) with an already-correct arrow is **not** touched — only a
  genuinely reversed arrow is. The check is direction-vs-labels, never
  node-write-order-vs-some-assumed-canonical-order.
- Explicitly out of scope, left untouched even when reversed: multi-type
  relationships (`[:A|B]`), variable-length paths (`[:R*1..3]`), and
  same-label relationships (`SUPERSEDED_BY: Regulation->Regulation`, where
  labels alone can't distinguish "from" from "to"). Same "can't correct what
  it can't verify, fail loudly rather than guess" instinct as
  `_assert_read_only` and `EntityResolver` elsewhere in this codebase — this
  is a narrow, auditable tool, not a general Cypher parser.
- Corrections are surfaced to the model via a `direction_corrected` key in
  `run_cypher`'s response, not applied silently — the model should see its
  own query had a reversed relationship the same way it sees a tool error,
  not have it invisibly fixed underneath it with no trace in the
  conversation.

## Build

`query_mechanism_v2.py`:
- `SCHEMA_RELATIONSHIP_DIRECTIONS` — the structured schema.
- `correct_relationship_directions(query)` — regex-based node/relationship
  parsing (no real Cypher grammar/AST; a targeted node-pattern regex plus a
  relationship-arrow regex, matched against consecutive node occurrences),
  returns the corrected query plus a list of `DirectionCorrection` records.
- `ToolBox.run_cypher` now calls this before `self.graph.query(...)`, and
  adds `direction_corrected` to its response dict when a correction fired.

No changes to `query_mechanism_v1.py` — its templates are authored,
parameterized, and already tested (39/39); this is scoped to `run_cypher`,
the one place freehand LLM-authored Cypher enters the system, per
`q-approach3.md`'s own scoping.

## Result

All existing suites still pass, unmodified: `test_query_mechanism_v1.py`
39/39, `test_query_mechanism_v2.py`'s pre-existing 22/22. 13 new tests added
to `test_query_mechanism_v2.py` (35/35 total), covering:

- The **real H1 and H11 reversed queries**, verbatim in shape, both corrected
  to the forward direction.
- An already-correct 5-hop chain (the exact shape `whole_graph_stats` uses)
  left byte-for-byte untouched.
- A legitimate backward traversal (Control → Standard with a correct arrow)
  left untouched — confirms this isn't a write-order heuristic in disguise.
- Unresolvable (unlabeled) hops, undirected relationships, multi-type
  relationships, variable-length paths, and the same-label `SUPERSEDED_BY`
  case — all left untouched, all deliberately out of scope per the design
  above.
- A `count(...)` call elsewhere in the query doesn't trigger a false-positive
  correction (the node-pattern regex is intentionally permissive and matches
  non-path parenthesized text too; the label-match gate is what keeps that
  safe, not the node regex's precision).
- `SCHEMA_RELATIONSHIP_DIRECTIONS` structurally matches every relationship
  named in `GRAPH_SCHEMA`'s prose.
- **End-to-end against live FalkorDB**: `ToolBox.run_cypher` given H1's exact
  reversed query now returns the same 3 real `Standard` rows the direct
  (correct) query returns — the specific silent-zero-rows failure that made
  H1 answer "non-compliant" instead of "partial" is closed, verified against
  live data, not just asserted against the string-rewrite logic in
  isolation.

## Empirical re-verification: does this move real-model outcomes?

The tests above prove the string-rewrite logic is correct in isolation. They
don't prove the fix matters in practice — that needs real model runs, not
another unit test. `experiment_direction_correction_rerun.py` re-ran the
exact 5 questions `q-approach2.md`'s "Result" table already has real-model
verdicts for (H9, H1, M14, H13, H11 on `qwen3:14b`; H1 and H11 again on
`qwen3-coder-next:q4_K_M`, the specific pairing where direction-reversal was
originally observed), through the live `QueryMechanismV2` agent loop with
the corrector now wired in, logging every `direction_corrected` firing so
outcome changes can be attributed rather than assumed.

**Direction corrections fired in 2 of 7 trials** — and on relationship types
*beyond* the two originally-documented bugs (`SUPPORTED_BY`, `DEFINES`):

- **H11 / `qwen3:14b`**: 1 correction, on `EXPRESSES` — a hop neither original
  failure touched. The corrector generalizing to a third relationship type in
  a live run is real evidence it isn't overfit to the two historical
  examples.
- **H1 / `qwen3-coder-next`**: 3 corrections, on `SATISFIED_BY` (twice) and
  `IMPLEMENTED_BY` — again, not the originally-observed `SUPPORTED_BY` hop.
  The prior run of this exact question had answered **non-compliant**
  (wrong) because a reversed `SUPPORTED_BY` zeroed real evidence. This run:
  **partial**, correctly citing real implemented Standards/Controls
  (encryption, access control, logging) alongside real gaps (an overdue SLA
  review control, a deprecated legacy policy) — a materially better verdict,
  directly attributable to the corrector firing mid-run. **Not fully
  correct against the golden rubric, though**: it states all 12 Art. 32
  obligations "have corresponding capabilities... governed by approved
  policies," which is false — `golden-answers.md`'s corrected H1 entry
  documents two sub-clauses (32.1, 32.1d) as **entirely ungoverned**, a gap
  category the rubric explicitly calls out as more serious than "stale."
  The direction fix turned a false negative into a better-but-still-
  incomplete answer, not a fully correct one — the remaining gap is a
  different failure (missing a governance edge that was never queried for),
  outside this fix's scope.

**Improvements observed with zero corrections firing were also observed —
and are not attributable to this fix.** H1/`qwen3:14b` produced a
detailed, mostly-accurate per-sub-clause breakdown this run (previously
failed twice on property names before ever reaching a hop this corrector
touches); H11/`qwen3-coder-next` converged in 3 tool calls this run instead
of exceeding a 16-turn cap without an answer. Neither run triggered a single
direction correction, so neither improvement can be credited to this fix —
both are consistent with the run-to-run variance `experiment_temperature.py`
and `experiment_self_consistency.py` already documented, not evidence this
fix reaches further than it does.

**Failure modes this fix doesn't touch are still present, unchanged, in this
same re-run.** H13 (routed through `whole_graph_stats`, no freehand
traversal, no direction risk at all) hallucinated different specific numbers
than its original run (this time: "two automated controls remain
unimplemented" against a real count of 1) while getting the headline GDPR
chain split right — the same synthesis-hallucination class flagged
originally, just a different fabricated number. H11/`qwen3-coder-next`,
even in its fast-converging run, found only 1 of the real 7 obligations and
said so honestly rather than fabricating the rest — a correct instance of
"don't guess," but still the "stopping early / under-citing" failure class,
fully unresolved.

**What this establishes, and what it doesn't.** For the specific failure
this fix targets — a reversed relationship silently zeroing real evidence —
there's now direct evidence it fires correctly against live model output, on
relationship types beyond the two it was built from, and that firing
measurably changed a wrong verdict into a better one at least once. It does
not touch, and this re-run confirms it does not touch, the larger population
of remaining failures: property/ID-pattern mistakes, incomplete multi-hop
exploration, and whole-graph synthesis hallucination. Of the 7 trials here,
1 clearly improved *because of* this fix; the rest either improved for
unrelated reasons or are still failing the same way they did before. This
closes `q-approach3.md`'s first reflection with real evidence behind it, not
just the unit-test proof above — and sharpens the case for its second,
still-open reflection: the dominant remaining failure category is
completeness/synthesis, not direction, which is exactly what
Plan-on-Graph-shaped adaptive exploration would target if it's worth
building for a graph this size.

This closes `q-approach3.md`'s first reflection. Its second reflection
(whether a Plan-on-Graph-shaped adaptive-breadth restructuring is worth
building for a graph this small) remains open and untouched by this doc.
