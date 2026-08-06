# Approach 3 (Reflections): What the Research Says About Approach 2's Gaps

**Scope note, upfront**: this document is reflections grounded in external
research, not a build. No code, no new experiment scripts, no changes to
`query_mechanism_v2.py` accompany it. It exists to answer a specific
question honestly — *has the problem approach 2's residual failures point
at already been solved by someone else?* — before spending more of this
spike's budget re-deriving fixes the field has already found, or chasing
fixes for a genuinely open problem as if it were a known one. If either
direction below gets picked up for real, it should get its own doc
following `q-approach1.md`/`q-approach2.md`'s pattern (spec, build, real
test results) rather than being retrofitted into this one.

## Recap: what approach 2 left unresolved

After the schema-grounding fix (`q-approach2.md`, "Grounding location
matters"), two residual failure classes remained, seen across the sampled
questions:

1. **Relationship-direction reversal** — `qwen3-coder-next` wrote
   `(pol)<-[:SUPPORTED_BY]-(:Standard)` and `(reg)<-[:DEFINES]-(role)`,
   both backwards from the schema it was given, on two different questions
   (H1, H11). Silent failure — no error, just zero rows, indistinguishable
   from "this doesn't exist" without independently knowing better.
2. **Stopping early / under-citing** — H1 answered "here's the chain we'd
   need to check" instead of checking it; H11 cited 3 of 7 real obligations
   in one run. Four combination experiments tried to close this: plain
   union-of-N (worked, mechanically — regex-extract cited ids across runs,
   take the union), temperature tuning (made things worse), a
   generator-validator critique loop (validator caught one real gap, but
   the fix didn't survive a controlled re-test, and the auto-revise loop
   never converged), and pooling evidence into one synthesis+validation
   pass (worse than plain union — an LLM synthesis step lost information
   that was mechanically present in its own context).

That second thread raised the actual question this document answers: is
"an LLM agent completely and correctly traverses a multi-hop graph" a
solved problem elsewhere, with a known fix we should be borrowing instead
of re-deriving through more prompt iteration?

## Is this solved? Partly — and the two halves are different in kind.

**The relationship-direction problem specifically: yes, solved, and not
by an LLM.** There's a public competition for exactly this —
[the Cypher Direction Competition](https://github.com/tomasonjo/cypher-direction-competition)
— and its winning approach ships in LangChain as
[`CypherQueryCorrector`](https://python.langchain.com/api_reference/community/chains/langchain_community.chains.graph_qa.cypher_utils.CypherQueryCorrector.html):
given the schema and a generated query, it deterministically parses and
corrects relationship directions by comparing against the schema's real
patterns. No model judgment involved at any point — a parser-and-compare
step, not a probabilistic mitigation. This is categorically different from
everything else tried in this spike so far (better prompting, more
sampling, a second model's opinion) — those all reduce the *rate* of a
mistake; this eliminates a specific mistake *class* by construction, the
same way `_annotate_trust()` in `query_mechanism_v1.py` moved the
trust-flag decision out of prose and into deterministic Python. Directly
relevant confirmation from the Text2Cypher research literature: "Cypher
imposes strict schema constraints and requires precise relationship
directionality, where even minor errors... can invalidate a query" and
models "mainly struggle with... relationship-direction" — this isn't a
`qwen3-coder-next`-specific weakness, it's a known, general LLM
weak point with a known, general fix.

**The broader "agent completely and correctly traverses a multi-hop KG"
problem: no, actively researched, not settled.** This exact problem shape
has a real lineage:

- [Think-on-Graph (ToG, 2023)](https://arxiv.org/abs/2307.07697) — LLM as
  agent, beam-search exploration of entities/relations, iteratively
  expanding and pruning reasoning paths. A formalized version of what
  `query_mechanism_v2.py`'s agent loop already does informally, just
  without beam search or path pruning — it commits to one linear
  tool-call sequence per question.
- [Plan-on-Graph (PoG, NeurIPS 2024)](https://arxiv.org/abs/2410.23875) —
  built specifically to fix what ToG doesn't: *adaptive* exploration
  breadth instead of a fixed, predefined search width, plus *self-correction
  of erroneous reasoning paths mid-exploration*. That maps almost exactly
  onto our failure mode 2 (H1 stopping after one call instead of adapting
  its exploration depth to what the question needed; H11 not
  self-correcting toward completeness). Also markedly cheaper than ToG —
  40.8% fewer LLM calls, ~76% less output-token cost — which matters given
  how expensive our own turn-heavy runs already were (some questions took
  12+ tool calls and still didn't converge).
- 2026 work (Inference-Scaled GraphRAG, entity-level-fusion approaches)
  reports concrete recall gains over naive baselines (one reports 0.874
  vs. 0.531 context recall, a 64.6% relative improvement) — real,
  measured progress, but framed by its own authors as *improvement over a
  baseline*, not *the completeness problem, solved*. The field's own
  framing matches what we found empirically: better, not solved.

**The validator/self-critique research explains our specific results,
but doesn't hand us a working validator.** Three findings map directly
onto what happened:

- [Self-Correction Bench (2025)](https://arxiv.org/abs/2507.02778) found a
  64.5% "self-correction blind spot" — models fail to correct errors in
  their *own* output while correctly fixing the identical error when
  attributed to an external source. Our validator experiments used the
  same model checking its own synthesis (fresh context, but same weights)
  — exactly the compromised setup this paper describes.
- Multiple studies on cross-model verification found "agreement bias and
  verifier error both decrease when the verifier is no longer the
  generator" — the fix implied by the finding above, not yet tested here.
- ["Lost in the middle"](https://arxiv.org/pdf/2510.10276) research shows
  LLM recall over long context follows a U-shape — 20–30 points worse for
  information sitting mid-context, rooted in RoPE's attention decay over
  distant tokens. Our pooled trace (28 concatenated tool-call results
  across 3 runs) is exactly the shape this predicts trouble for — whichever
  obligation ids happened to land mid-trace were structurally
  disadvantaged, independent of prompt quality.
- The broader LLM-as-judge literature (a March 2026 RAND reliability
  harness finding no judge uniformly reliable across benchmarks; a study
  nicknaming a judge setup "the Coin Flip Judge" over low run-to-run
  agreement) confirms our result — a validator PASSing a materially
  incomplete answer twice — is a documented category of failure, not
  something peculiar to our prompt or model choice.

None of this hands us a validator we can trust unsupervised. It explains
*why* ours didn't work and names the two changes with actual evidence
behind them (different model as verifier; restructure how evidence is
presented rather than dumping a raw trace) — neither of which has been
tested here yet.

## Reflection: two different next steps, not one

**1. Deterministic direction correction — adopt, don't re-research.**
High confidence this transfers to our project, because it isn't a
model-behavior fix at all — it's a parse-and-compare step against a
schema we already have fully specified (`GRAPH_SCHEMA` in
`query_mechanism_v2.py`). Lowest-risk, most clearly justified next
implementation whenever wiring resumes: validate (and where unambiguous,
correct) relationship direction in `run_cypher` before a query ever
reaches FalkorDB, the same category of fix `_annotate_trust()` already
represents elsewhere in this codebase.

**2. Adaptive-breadth planning (Plan-on-Graph-shaped) — the genuinely open
part, and genuinely uncertain here.** ToG and PoG were built and
benchmarked against large, sparse, real-world knowledge graphs (Freebase,
WikiData — thousands of relation types, millions of entities). Our graph
is 8 node labels, 8 edge types, ~700 nodes. Whether their beam-search/
path-self-correction machinery is the right tool for a graph this small
and this structured, or whether it's solving a scale problem we don't
have, is an open question this document doesn't answer — it would need
real prototyping against our own catalog to find out, not an assumption
either way based on where the papers were validated.

**3. What this research argues against continuing to chase**: further
prompt-only iteration on a same-model validator. The self-correction
blind-spot finding suggests there's a ceiling same-model validation hits
that more instruction-writing doesn't move past (consistent with what we
already found: adding the full schema to the validator's prompt didn't
change its verdict at all in a controlled test). And restructuring how
pooled evidence is presented to fight lost-in-the-middle effects is a
more promising lever than continuing to refine the validator's checklist
wording.

## Open questions for whoever picks this up next

- Is a Plan-on-Graph-shaped restructuring solving a problem this graph's
  scale actually has, or importing complexity benchmarked on a different
  problem? Worth testing directly before committing to the rebuild.
- Does deterministic direction-correction alone (cheap, low-risk, adopt
  today) close enough of the gap that the harder architectural question
  above doesn't need answering soon?
- If cross-model verification gets tested (a concrete, cheap experiment
  given Ollama already has multiple model families downloaded): does it
  actually outperform same-model validation on *this* graph and *these*
  questions, or does the self-correction-blind-spot literature's finding
  fail to transfer the way the schema-grounding fix didn't transfer from
  generator to validator?
