# Approach 2: Agentic Tool-Use Over the Live Graph

Scoped by what `q-approach1.md`'s template router genuinely cannot do — not
the whole catalog. After the second pass added Software Engineer / Security
Engineer / Engineering Manager questions to `example-questions.md`, that's
**15 questions**: M3, M5, M14, H1, H3, H5, H6, H8, H9, H11, H12, H13, H14
need semantic reasoning a fixed template can't produce; H10 and H15 are
schema gaps no query mechanism can close (flagged, not solved, below).

## Why not build this on a GraphRAG framework

The original framework survey (see the conversation this doc follows from)
identified FalkorDB's own `GraphRAG-SDK` as the closest-fit candidate —
same vendor as the DB already in use, and web documentation described it as
supporting both **Local Search** (entity-anchored traversal) and **Global
Search** (community-detection + hierarchical LLM summaries, the Microsoft
GraphRAG pattern) directly against an existing ontology.

That turned out not to match the installed package. `pip install
graphrag-sdk` (1.3.0, confirmed installable and installed for this
inspection — see below) exposes a `GraphRAG` class whose only retrieval
modes are `SearchType.VECTOR`, `SearchType.FULLTEXT`, and `SearchType.HYBRID`
— flat embedding/keyword retrieval over ingested text chunks, not
graph-native Cypher traversal. There is no `KnowledgeGraph` class, no
`.ask()`/`.chat_session()`, and grepping the installed package source for
`community` or `global_search` finds nothing — no Leiden clustering, no
community summarization anywhere in the shipped code. `Ontology.from_sources()`
takes raw documents and an LLM to *extract* a schema; there's no path for
handing it a schema that describes an *already-populated* property graph
and using that purely for query-time entity/relationship typing.

In short: the installed SDK is a document-ingestion + hybrid-chunk-RAG
package. Feeding it our already-structured, already-extracted Policy System
graph would mean either (a) re-deriving text chunks from our structured
nodes just to hand them back to its extraction pipeline — backwards, since
extraction is exactly what already happened — or (b) using only its
`FalkorDBConnection`/`ConnectionConfig` plumbing and none of its retrieval
logic, at which point we're not meaningfully "using the SDK." This is
exactly the "verify by running it, not by trusting docs" risk flagged
before committing to it — worth surfacing prominently since it reverses the
earlier framework recommendation, not just a footnote.

**What's still worth keeping**: `graphrag_sdk.LLMInterface` is a clean,
minimal `invoke(prompt) -> LLMResponse` ABC with sync/async variants — a
reasonable shape to imitate for a pluggable LLM seam. `query_mechanism_v2.py`
defines its own equivalent (`LLMClient` Protocol) rather than importing
`graphrag_sdk` itself, though, to avoid pulling in its ~10 heavy ML
dependencies (`torch`, `transformers`, `gliner`, `onnxruntime`...) at import
time for a package whose actual retrieval/ingestion machinery isn't used.

**Conclusion**: don't build approach 2 on a GraphRAG framework. Property
graphs with a real, already-correct schema are better served by giving an
LLM the ability to write and run Cypher directly against that schema than
by routing through machinery built to *construct* a graph from raw text.
This is "agentic tool-use," the fourth option named in the original
framework survey, not a failure to find a reusable framework — the
research is what tells us tool-use is the right fit here, not a fallback.

## Design

**Reuse, don't re-route.** `query_mechanism_v1.py`'s `QueryMechanismV1.ask()`
runs first, unchanged, imported directly. If a template matches, that's the
answer — zero LLM cost, fully deterministic, exactly the floor approach 1
established. Approach 2 only engages on `NoTemplateMatch`.

**One agent, three tools, not a hand-written local/scoped/global classifier.**
An earlier version of this design (see conversation history) proposed
classifying each question into anchored/scoped-aggregate/global buckets
before deciding how to answer it. Building the entity-resolution step first
showed that classification to be unnecessary *and* actively wrong for
several real questions: H1 ("Article 32") and H6 ("Software Bill of
Materials") both *read* as anchored to a human, but `query_mechanism_v1`'s
deliberately-unambitious substring `EntityResolver` fails to resolve either
of them — "software bill of materials" isn't a substring of "Component
Inventory & SBOM Management" in either direction, and "MFA" isn't a
substring of "Access Control & Authentication." A hard-coded classifier
built on that resolver would misfile both as "global" and lose the
precision a real anchored walk gives once the *right* entity is found. An
agentic loop sidesteps this: instead of deciding scope up front, give the
model tools to discover its own scope, and let how many tool calls it needs
be the emergent signal, not a pre-classification.

Three tools, given to the LLM alongside the domain schema
(`ps-domain-concepts.md`'s 8 node labels / 8 edge types) as system context:

1. **`list_entities(label)`** — returns every real `name`/`title`/`id` for a
   node label (`Role`, `Capability`, `Policy`, `Regulation`). This is what
   closes the "MFA" / "SBOM" / "PII" gap: rather than a substring match
   failing silently, the model can browse the real vocabulary and pick the
   closest real match itself (or correctly report no match — see H9/H10
   below), the same honesty obligation `EntityResolver` already enforces,
   just delegated to semantic judgment where substring matching is too
   blunt an instrument.
2. **`run_cypher(query)`** — executes a **read-only** query against the live
   `policy_system` graph and returns rows. This is the actual retrieval
   step, replacing GraphRAG-SDK's mismatched chunk-retrieval with the
   graph's own native query language, which is what actually fits a
   property graph with a known schema. Enforced read-only at the client
   (reject anything not starting with `MATCH`/`OPTIONAL MATCH`/`RETURN`
   after stripping `WITH`/`WHERE` clauses) — an LLM-authored `DELETE` or
   `MERGE` against the live graph is not a risk worth taking for a query
   mechanism.
3. **`whole_graph_stats()`** — returns a fixed, pre-computed bundle of the
   deterministic aggregate facts the "global" questions (H12, H13, H14) need
   to narrate: governed/ungoverned capability counts (H2's 55/68), Policy
   status breakdown (M10's 2 approved/1 draft/1 deprecated), overdue/planned
   Control counts (M9/M12's data), and the M11 zero-implemented-Control
   capability set. **Deliberately not delegated to the model's own
   `run_cypher` calls.** `q-approach1.md` already found a real FalkorDB
   correctness issue where a 6-hop `MATCH`'s row count silently changed
   with `RETURN` projection choice — asking a model to freehand a
   whole-graph gap-scan query risks the same class of silent error, just
   authored by the model instead of a human. Pre-computing these numbers in
   tested Python (reusing the exact queries already verified against golden
   in `test_query_mechanism_v1.py`) means the model's only job for global
   questions is narration, never arithmetic over unverified rows.

**Trust-flag discipline carries through by construction, not convention.**
`whole_graph_stats()`'s numbers are already governance-aware (the "governed"
count only counts `GOVERNED_BY` edges, `is_current_evidence`-style logic is
baked into which Controls count as "implemented" for the gap sets) —
there's no separate step where an LLM could launder a `deprecated` Policy
into confident prose, because the deprecated/draft signal is already a
labeled field in the data it's handed, not something it has to notice and
choose to mention. For the `run_cypher` path (single-entity chains, e.g.
H1's Article 32 walk), the system prompt requires the model to compute and
state the same `p.status`/`s.implementation_status`/`ctrl.implementation_status`
combination `_annotate_trust()` computes in `query_mechanism_v1.py` for
every chain it retrieves — same rule, enforced by instruction here since
the model (not fixed Python) is writing the query.

**Honesty on empty results is a system-prompt requirement, not a hope.**
The model is instructed: if `list_entities` finds no plausible match and
`run_cypher` returns no rows, say so explicitly — never fill the gap with a
plausible-sounding guess. This is what H9 (no rate-limiting capability
exists) and H10 (no Service node exists at all) need, and it's the same
"fail loudly rather than guess" principle `NoTemplateMatch` already
enforces mechanically in approach 1, now asked of the model as an
instruction instead of guaranteed by code. That's a real weakening — worth
being honest about in the test plan below, not papered over.

## Schema gaps this approach does not attempt to solve

H10 ("is my service compliant") and H15 ("how long does a Standard take to
go from draft to implemented") are not routing problems. No tool given to
an arbitrarily capable model closes them, because the graph has no `Service`
node and no status-transition history — see `golden-answers.md`'s H10/H15
entries. The correct behavior for both is the same honest refusal
`NO_TEMPLATE_MATCH` already gives structurally; approach 2's job is to
*state that refusal in prose* (per the honesty requirement above), not to
find a clever way around a missing concept in the domain model.

## Environment constraint — resolved via local Ollama

No cloud LLM provider credential (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
etc.) is configured in this environment. But a local Ollama server is
running (`127.0.0.1:11434`, 17 downloaded models) and its OpenAI-compatible
endpoint supports real tool-calling — confirmed with a direct round-trip
before committing to it. `OllamaClient` in `query_mechanism_v2.py`
implements `LLMClient` against that endpoint via the `openai` Python
package (already installed; lazy-imported so the rest of the module has no
hard dependency on it). `NoLLMConfigured` stays the default for anyone
without a local model available, and still fails loudly rather than
silently returning nothing.

**Not routed through `pi`** (a general-purpose coding-agent CLI also
available in this environment, already configured with Ollama as a
provider). `pi`'s tool set is fixed — `read`/`bash`/`edit`/`write`/`grep`/
`find`/`ls`, aimed at repo editing — with no mechanism found for adding
custom typed tools (no MCP support in the installed build). Giving it our
three FalkorDB tools would mean writing a CLI wrapper around `ToolBox` and
having the model shell out to it through `bash`, then parse text back —
strictly worse than native structured tool-calling (no JSON-schema
validation, an extra text-parsing failure surface, no gain over what we
already have). The actual "harness" this problem needs — message-history
threading, tool dispatch, a turn limit, the read-only guard — is what
`QueryMechanismV2._ask_agent` already is, built and plumbing-tested before
this section existed. Reaching for `pi` here would have been the same
mistake as reaching for GraphRAG-SDK's ingestion pipeline earlier: adopting
a real, general-purpose tool built for a different shape of problem instead
of the thin, purpose-fit one already in hand.

## Result: real runs against a local model, not just plumbing

`test_query_mechanism_v2.py` still covers the plumbing (22/22, scripted
`FakeLLMClient`, no live model needed). Beyond that, `OllamaClient` was run
for real against 5 of the 13 targeted questions (`qwen3:14b` unless noted),
8 total calls including two prompt-fix iterations — not the full 13 (cost:
each call is 6–90s of local inference; this is a sampled, honest read, not
a full grading pass):

| Question | Result | What happened |
|---|---|---|
| H9 (no rate-limiting capability) | 🟡 partial | Correct conclusion (no such capability, no fabricated verdict) but arrived at it by guessing an exact-match literal (`cap.name = 'rate-limiting'`) rather than checking the `list_entities` result it had already fetched — right answer, slightly lucky path. |
| H1 (GDPR Art. 32 compliance), attempt 1 | ❌ fail | Used `Regulation.name` and `Requirement.title` — neither property exists (schema is `id`/`text`) — got zero rows and concluded "not tracked," a false negative. Real answer: 4 real sub-clause chains exist (32.1a/b clean, 32.1c partial, 32.4 stale). |
| H1, attempt 2 (after adding exact property names + an anti-absence-conclusion instruction to the prompt) | ❌ still fails, different cause | Fixed the property names but used `req.id ENDS WITH 'art_32'` — real ids are `..._art_32.1a`/`32.1b`/`32.1c`/`32.4`, none of which end with the bare number — zero rows again, same false-negative conclusion despite the new instruction explicitly telling it not to do that. |
| M14 (draft Policy blocking GDPR), attempt 1 | ❌ fail | Same root cause as H1 attempt 1 (`Regulation.title` doesn't exist). False "no draft Policies" negative. |
| M14, attempt 2 (same prompt fix as H1) | ✅ pass | Correct property (`Regulation.id`), correct chain, named the real Policy (`pol_clinical_data_integrity_policy_e1a539`) and correctly caveated its draft status. **The schema fix worked cleanly here** — a real, validated improvement, not just a plausible-sounding one. |
| H13 (board summary, global) | 🟡 partial | Called `whole_graph_stats` correctly (as instructed) and got most numbers right (55/68 ungoverned, the real 4-capability M11 set, the 1 planned control). But reported "2 controls overdue" when the tool it just called returned 1, and phrased the 57/31/26 GDPR-chain split ambiguously enough to read as self-contradictory. **Hallucinated a specific number while holding the correct one in hand** — a synthesis failure, not a retrieval one. |
| H11 (MFA, backward multi-hop), attempt 1 | 🟡 partial | The Cypher it wrote was actually correct and returned all 7 real obligations (confirmed by re-running its exact query directly). But its final answer discussed only 2 of the 7 and claimed the data didn't link the rest to specific regulations — it had the complete right answer in the tool result and reported an incomplete one. |
| H11, attempt 2 (after adding a "don't drop retrieved rows" instruction) | ❌ fails differently | Used `cap.id = 'Access Control & Authentication'` (a capability *name*, used as an *id* filter) despite the schema explicitly distinguishing `id`/`name` and despite having just called `list_entities('Capability')`, which returns names. Zero rows, false negative again, though more hedged in tone than attempt 1. |
| H1, `qwen2.5-coder:14b` (one cross-model comparison) | ❌ fails at the protocol level | The Cypher it constructed was arguably *better* than `qwen3:14b`'s — correct properties, even included the trust-flag-relevant `pol.status`/`con.implementation_status` filter unprompted. But it emitted the tool call as JSON *text* in its response instead of using the API's structured tool-call field, so the agent loop (correctly, per the `LLMClient` contract) treated that JSON blob as a final answer and returned it verbatim instead of ever executing the query. |
| H1, `qwen3-coder-next:q4_K_M` (larger model, user-recommended) | 🟡 partial, notably better process | Genuinely impressive self-correction: its first query (`ENDS WITH 'art_32'`) also came back empty, and unlike either 14B model it **retried on its own** with `CONTAINS`, discovered the real requirement set is *six* ids (`32.1`, `32.1a`–`32.1d`, `32.4`) — two more than `golden-answers.md` itself had on record (see correction above, found because of this run) — and walked the full Requirement→Obligation→Capability→Policy chain across all six via batched `IN`-list queries. But its final Policy→Standard→Control query used `(pol)<-[:SUPPORTED_BY]-(:Standard)` — the relationship **reversed** from the schema it was given (`Policy-[:SUPPORTED_BY]->Standard`) — which silently zeroed out real evidence (`cap_data_encryption_0e50d3` alone has 3 real `implemented` Controls, confirmed by re-running the corrected direction directly), so it concluded "no implemented Controls anywhere" and answered **non-compliant** — too pessimistic; correct is **partial** (worse, not better, than `qwen3:14b`'s failure mode in one sense: this one is evidenced and reasoned through carefully, then wrong at the last hop). |
| H11, `qwen3-coder-next:q4_K_M` | ❌ fails — never converged | 12 tool calls, mostly legitimate exploratory recovery from dead ends (checked whether "MFA" was literally in a capability name/description, backed off to broader property search, eventually found `cap_access_control_authentication_151816` correctly). But repeatedly wrote `(reg:Regulation)<-[:DEFINES]-(role:Role)` — again the **reverse** of the schema's `Regulation-[:DEFINES]->Role` — got empty results from that mistake several times over, and never diagnosed the pattern. Exceeded even a raised 16-turn cap (default is 8) without producing a final answer. |

**What this shows, plainly:**

1. **The mechanism itself never failed.** Across all 8 calls, tool
   dispatch, message threading, the read-only guard, and loop termination
   worked correctly every time. Every failure above is an LLM accuracy
   problem, not a plumbing bug — `test_query_mechanism_v2.py`'s 22/22 was
   the right thing to have verified separately from model quality, per its
   own stated scope.
2. **Two distinct, separately-diagnosed LLM failure modes**, not one: (a)
   wrong Cypher property or ID-pattern guesses producing a false "not in
   the graph" conclusion, which an explicit schema cheat-sheet fixed for
   one case (M14) and left unfixed for a structurally different variant of
   the same class (H1's multi-clause article ids, H11 attempt 2's
   name-vs-id mixup); (b) correct retrieval followed by inaccurate or
   incomplete synthesis of the model's own tool results (H13's
   hallucinated count, H11 attempt 1's dropped rows) — an explicit
   instruction against this was added but not cleanly re-verified, since
   H11's retest failed for an unrelated (class-(a)) reason instead.
3. **Tool-calling protocol compliance is its own axis, separate from
   reasoning quality**, and it isn't safe to assume a model that accepts an
   OpenAI-style `tools` parameter without erroring will actually use it.
   `qwen2.5-coder:14b` wrote better Cypher than `qwen3:14b` in the one
   head-to-head and still produced a functionally worse outcome, because it
   didn't emit the tool call the way the protocol expects.
4. **A larger, tool-calling-strong model (`qwen3-coder-next:q4_K_M`,
   user-recommended) is a genuinely different tier of *process* — and still
   gets the wrong answer.** It self-corrected on the ID-pattern mistake that
   sank two `qwen3:14b` attempts, without being told to, and in doing so
   found a real gap in `golden-answers.md` itself (see the H1 correction
   above — a live example of exactly the kind of independent verification
   this whole spike has been trying to instill). But it introduced a *new*
   failure class neither 14B model hit: **relationship direction reversed**
   (`(pol)<-[:SUPPORTED_BY]-(:Standard)` instead of the schema's
   `Policy-[:SUPPORTED_BY]->Standard`) — silently zeroing out real evidence
   the same way a wrong property name does, but harder to catch by reading
   the query, since the property names and node labels are all correct.
   This happened twice, independently, across two different questions
   (H1's Policy→Standard hop, H11's Regulation→Role hop), which makes it
   look like a systematic weak point rather than a one-off, not something a
   single schema cheat-sheet fix (which already states the correct arrow
   direction) resolved.
5. **Thoroughness has a turn-budget cost that isn't free to raise.** H11
   against `qwen3-coder-next` made 12 genuinely-productive-looking tool
   calls and still hadn't converged at 16 turns (double the 8-turn
   default) — this model's careful, self-correcting style is exactly what
   you'd want for accuracy, but it means turn limits tuned for a faster,
   less careful model will cut off a more careful one before it finishes,
   not just before it "gives up."
6. **This makes the approach 1 vs. approach 2 comparison an evidenced one,
   not an assumption.** The free, deterministic 24/39 template floor is now
   known to be strictly more reliable, in this sample, than either a
   14B-class or a larger local model's agentic Cypher-writing — approach
   1's own thesis ("this is the honest floor a fancier mechanism needs to
   justify itself against") holds up under an actual test, not just the
   prediction it started as. Bigger and more careful narrowed *which*
   mistake got made, not whether one did.

## Grounding location matters — tested, not just theorized

All of the above ran with the graph schema (property names, relationship
directions) living only inside the `run_cypher` tool's JSON-schema
`description` field — available to the model, but not part of the
system prompt's primary instructional text, and easy for a model to
under-weight relative to system-role content, especially as tool-result
history grows across turns. Raised as a hypothesis after reviewing the
H1/H11 failures: normally you'd ground a model in the problem *before*
asking anything, not leave it to notice schema details buried in a tool
argument's description.

Tested directly, not just adopted on the strength of the argument:
`SYSTEM_PROMPT` now carries the full schema (labels, relationship
directions with an explicit "direction matters, wrong direction returns
zero rows silently, it does not error" warning, exact properties per
label, and a general note on matching id families like an article's
lettered sub-clauses) as its own section, re-read every turn; the
`run_cypher` tool description shrank to a one-line pointer back to it. One
more fix alongside this: the schema text previously included a *specific*
worked example — `req.id ENDS WITH "art_32.1a"` — sitting right next to
the exact case that needed a *general* multi-clause match, which was a
plausible direct contributor to the `ENDS WITH 'art_32'` mistake both
models made independent of where the text lived. Replaced with a
generalized version using a different article number, so re-testing H1
afterward isn't just the model echoing an answer it was handed.

**Re-ran H1 (`qwen3:14b`) and H11 (`qwen3-coder-next:q4_K_M`) after the change:**

- **H1**: immediately wrote `req.id STARTS WITH 'GDPR-1.0_req_art_32'` —
  the ID-pattern mistake that sank both earlier attempts didn't recur. But
  it stopped after that one call and answered "we don't have enough
  evidence to confirm compliance, here's the chain we'd need to check
  next" instead of continuing to check it — a real improvement (no more
  confidently-wrong false negative) but not yet a complete answer. A
  different, milder failure: honest underexploration instead of
  overconfident wrongness.
- **H11**: no relationship-direction reversal anywhere across 7 tool calls
  (previously reversed `DEFINES` repeatedly and never finished). Explored
  several real candidate framings, self-corrected a mid-stream syntax
  typo, and landed on a well-hedged answer citing 3 of the real 7
  obligation ids by name (NIS2's two explicit MFA obligations plus CRA's
  unauthorised-access one), correctly noting GDPR's and CRA's broader
  access-control obligations exist without fabricating their ids. Still
  short of a full pass against `golden-answers.md`'s rubric — didn't
  enumerate all 7 by id, and never mentioned that the capability is
  currently governed with an *implemented* Control (the "this is
  hypothetical against real current evidence" point the rubric asks for)
  — but a clear, evidenced step up from not converging in 16 turns.

**Conclusion: the hypothesis held, concretely, not just plausibly.** Moving
the schema into the system prompt — and fixing one bad example sitting
next to it — eliminated the ID-pattern and relationship-direction failures
in both of the two cases they were previously observed in. What's left is
a shallower problem: thoroughness (H1 stopping early) and completeness
(H11 not citing every real row), not wrong-Cypher-shape mistakes. Worth
carrying forward as a standing design rule for this mechanism: **schema
grounding belongs in the system prompt, not in a tool's argument
description** — and any worked example placed near instructions for a
*general* case should itself be general, not a specific instance of the
exact question being tested.

## Further experiments: self-consistency, temperature, generator-critic

After the grounding fix, the remaining residual failure on H11 (correct
tool calls, but under-citing — 3 of 7 real obligations in one run) raised a
natural question: would sampling more, tuning decoding, or adding a second
model as a check close that gap? Three ideas, each tested directly against
H11 (chosen because it has an objective, checkable metric — 7 real
obligation ids confirmed against the live graph — unlike the rubric
questions, which need human judgment to grade). **None of these are wired
into `QueryMechanismV2` or `OllamaClient`** — each lives in its own
standalone script (`experiment_self_consistency.py`,
`experiment_temperature.py`, `experiment_validator_loop.py`), deliberately
kept separate so the shipped mechanism doesn't change based on a
small-sample experiment. Every number below is a real run against
`qwen3-coder-next:q4_K_M`, not a projection.

### Self-consistency: sample N times and combine (`experiment_self_consistency.py`)

Three independent runs, same model, same prompt:

| Run | Tool calls | Obligations cited |
|---|---|---|
| 1 | 7 | **7/7** — fully correct |
| 2 | 9 | 3/7 |
| 3 | (hit the turn cap) | 0/7 — no answer produced |

**Union across the 3 runs: 7/7. Intersection (strict consensus): 0/7.**

The finding is in which combination strategy works, not just that sampling
helps. Classic self-consistency (Wang et al.) assumes most samples cluster
near a correct answer and a majority vote filters out the odd one out —
that assumption doesn't hold here: the three runs didn't cluster, they
spanned "fully correct" to "nothing at all," so a strict-agreement vote
would have discarded the one genuinely complete answer. Union (or
best-of-N by whichever run made the most tool calls) is the strategy with
real evidence behind it here; a naive majority-vote implementation would
have been worse than picking any single run at random.

Cost is real, not theoretical: the 3 runs took roughly 56s combined
against ~15–20s for one run, and one of the three contributed nothing.

### Temperature: does lowering it reduce the variance self-consistency exploited? (`experiment_temperature.py`)

Naive expectation: yes — lower temperature, more deterministic, fewer bad
turns. Tested directly rather than assumed, same question, 3 runs per
setting:

| Temperature | Results (3 runs) |
|---|---|
| default (unset) | 7/7, 3/7, timeout |
| 0.1 | 7/7, timeout, timeout |
| 0.0 | timeout, timeout, timeout |

**The opposite held.** Lower temperature made outcomes worse in this
sample, and the direction was consistent across two separate low-temperature
settings (not one noisy batch). Plausible mechanism: near-greedy decoding
on a multi-step agentic task can get stuck cycling through slight
variations of the same unproductive query, where moderate sampling
randomness is what lets the model jump to a genuinely different approach
when the current one isn't working. Small sample (n=3 per setting) — not
proof this holds everywhere — but a clear enough, consistent-enough signal
that "just lower the temperature" should not be assumed as a free win for
this kind of task.

### Generator + validator critique loop (`experiment_validator_loop.py`)

Same model as both generator and validator (fresh, independent call for
the validator — no shared context with the generator's conversation), the
validator sees the full tool-call trace (queries *and* the rows they
returned, not just the draft answer) and either passes it or returns
specific feedback, looping until PASS or a round cap:

| Run | Round 1 | Round 2 |
|---|---|---|
| 1 | 5/7 cited, and falsely claimed no CRA obligation names MFA — **validator correctly FAILed it** with accurate, specific feedback | revision attempt never converged — ran out of turn budget mid-revision |
| 2 | generator never converged at all | — |

Two separable findings, one positive and one not: the **validator itself
worked** — it caught a real, specific gap rather than rubber-stamping a
plausible-looking answer, real evidence that an independent second pass
catches what a generating pass didn't self-catch (the same phenomenon
self-consistency's variance points at, but via a targeted check instead of
blind resampling). The **revise-and-loop mechanism didn't pay off** in
this sample — feeding the critique back into the same conversation, competing
for the same turn budget as the original exploration, turned a mediocre
but real 5/7 answer into no final answer at all in run 1. That looks like
a design problem (revision sharing a budget with exploration) rather than
evidence the concept is unworkable — an untested next step, not a dead end:
give the revision attempt its own fresh turn budget, or have the validator's
feedback trigger one narrow, targeted follow-up call ("fetch this
specific missing thing") instead of "revise your whole answer."

### Combining union and validator: pool the evidence, one synthesis + one validation pass (`experiment_union_plus_validator.py`)

The obvious next question, asked directly rather than assumed: does
combining the two ideas that each showed partial promise do better than
either alone? Design: run the generator 3 independent times (no validation
in between, same as self-consistency), pool every tool call and result
from all 3 runs into one merged evidence set, then **one** synthesis call
(no tools, forced to answer only from the pooled evidence — avoiding the
open-ended re-exploration that ate the revise loop's turn budget), then
**one** validation call against that synthesis. Bounded, predictable cost:
3 generator runs plus exactly 2 more calls, no loop.

One real run: the 3 generators produced 0/7, 7/7, and a non-convergent
0/7 — pooling their *answer-level* citations reaches 7/7, same union
result as the plain self-consistency experiment. But the synthesis call,
despite having all 28 pooled tool-call results available — including the
exact evidence that let run 2 reach 7/7 on its own — only extracted **4/7**
into its answer. And the validator, which correctly caught a real 5/7 gap
in the standalone validator experiment above, **PASSed this 4/7 answer
without complaint.**

That's two independent slippages in one run: an LLM synthesis step lost
information that was mechanically present in its own context, and the
validator wasn't reliably strict the second time it was asked to do the
same job it had done correctly before. Net effect: this four-call
combination produced a *worse*, falsely-validated answer than simply
extracting each run's cited ids with a regex and taking the union — which
needs zero extra LLM calls. The lesson generalizes past this one
experiment: **pooling raw evidence helps, but handing the last mile —
turning pooled evidence into one final answer — to more LLM judgment
doesn't reliably realize that benefit.** Consistent with this whole
spike's recurring theme (`_annotate_trust()` in `query_mechanism_v1.py`,
the trust-flag discipline throughout): structural/mechanical combination
beats prose synthesis wherever it's possible to avoid the latter, and that
holds even when the "structure" is as simple as a regex over cited ids
instead of another model call.

### Was the validator cold-called? (`experiment_validator_grounding.py`)

Fair question raised directly against the finding above: `VALIDATOR_SYSTEM`
(defined in `experiment_validator_loop.py`) gives the validator a 3-item
checklist but, unlike the generator's `SYSTEM_PROMPT`, never gave it
`GRAPH_SCHEMA` — the exact fix that had just eliminated the generator's
property-name and relationship-direction failures. Reasonable to suspect
the validator's false PASS above had the same root cause.

Refactored `GRAPH_SCHEMA` out of `query_mechanism_v2.py`'s `SYSTEM_PROMPT`
into its own constant (pure extraction, verified byte-identical, no change
to the shipped mechanism's behavior — both test suites still pass 39/39 /
22/22 after it) so it could be reused verbatim rather than copy-pasted, and
added `VALIDATOR_SYSTEM_GROUNDED` — identical checklist, `GRAPH_SCHEMA`
inserted, plus one added line pointing the validator at the schema to
recognize which retrieved ids are the relevant kind. Tested as a
**controlled comparison**, not a fresh independent run: one pooled-evidence
synthesis (3 generator runs + 1 synthesis call, same recipe as the
experiment above) produced a single fixed trace and a fixed 5/7 draft
answer, then that *identical* input was validated twice — once with each
prompt — isolating the one variable that changed.

**Both PASSed the same incomplete 5/7 answer.** Adding the schema moved
the verdict not at all in this comparison.

That's informative on its own, not just an unhelpful negative: it argues
the validator's unreliability probably isn't the same failure class the
generator had. The generator's problem was a **knowledge gap** — it wrote
Cypher against the wrong property names or the wrong edge direction
because it didn't reliably have the right facts in front of it, and giving
it those facts fixed it. The validator's problem looks more like an
**attention/completeness-auditing gap** — correctly recognizing "here are
6 retrieved rows, does the 5-item answer account for all of them" over a
long, information-dense trace is a different cognitive task than knowing a
property name, and schema knowledge doesn't obviously help with it. A
fix that resolves one failure class doesn't automatically transfer to a
different one just because both involve "give the model more context" —
worth stating plainly since it's a easy mistake to make by analogy. One
controlled comparison, not a powered study — but it argues against
assuming the grounding fix generalizes, not for it.

### Citation completeness as a deterministic post-check (`experiment_citation_completeness.py`)

The cheaper alternative flagged in "Next" item 3: instead of another LLM
call, a regex extracts every real entity id (`role_`/`cap_`/`obl_`/`pol_`/
`std_`/`ctrl_` prefixes, plus Regulation/Requirement ids) out of a tool
result, and a set-difference against the final answer text reports which
ids never got mentioned — the same "annotate structurally, don't leave it
to prose" move `_annotate_trust()` and the direction corrector already
represent, at zero added LLM cost. Tested in two phases before any adoption
call, per this doc's own standard: 6 synthetic self-tests against real ids
pulled from `golden-answers.md` (extraction/comparison logic proven correct
in isolation first), then a live re-run of the exact same 7 (question,
model) trials `direction-correction.md`'s empirical re-run already used,
under three scoping variants — naive (last tool call of any kind),
run_cypher-only (last call), and run_cypher-union (every id across every
`run_cypher` call in the trace).

**One clean true positive, and it required the union variant, not the
literal "last tool result" spec.** H11/`qwen3-coder-next` cited 5 of the 7
real obligations in its final answer; the run_cypher-union check correctly
flagged the 2 missing ones (`obl_maintain_human_resources_security_
access_control_and_asset_m_644c45`/`..._40eba8`) — ids the model had
genuinely retrieved earlier in its 9-call trace and then dropped at
synthesis time, exactly the failure class this check targets. The
"last tool result" scoping literally proposed in "Next" item 3 would have
missed this entirely: this trial's last call already had 2/2 cited, giving
a false-clean read.

**"Last tool result" is an unreliable proxy for a multi-call trace, and
this is now evidenced rather than assumed.** H1/`qwen3-coder-next` made 7
`run_cypher` calls; its actual last one turned out to be an unrelated
tangent querying draft-Policy data (M14-shaped ids —
`cap_clinical_trial_data_integrity_f28d55`,
`pol_clinical_data_integrity_policy_e1a539` — nothing to do with Article
32). Checking against only the last call flags those irrelevant ids as
"missing" and says nothing useful about the actual Article 32 chain the
question was about. The union-across-the-trace variant is the one that
produces a signal connected to the question at all here.

**The naive variant (any tool type) false-positives hard on
`whole_graph_stats`-routed questions, confirmed live, not just in the
synthetic self-test.** H13 routed through `whole_graph_stats` only (no
`run_cypher` calls); its answer is a grounded-in-counts narrative that
never needed to name individual control/policy ids at all — exactly what
its golden rubric asks for. The naive check flagged all 8 incidental ids in
that pre-computed aggregate as "missing," a false alarm on an otherwise
reasonable answer. Scoping strictly to `run_cypher` (which SYSTEM_PROMPT's
rule 6 — "when a query returns multiple rows, your answer must account for
all of them" — is actually about) correctly abstains here instead.

**Exact-substring matching over-flags legitimate citations, and this is
the check's deepest problem.** SYSTEM_PROMPT rule 4 explicitly permits
citing "real ids/names," not ids exclusively. Both H1 runs cite entities by
descriptive name (*"Pseudonymisation/Encryption"*) or abbreviated form
(*"art_32.1a"* instead of `GDPR-1.0_req_art_32.1a`;
`cap_access_control_authentication` with the hash suffix silently dropped)
rather than the literal id string — legitimate per the prompt's own rules,
invisible to a substring check. H1/`qwen3:14b` flags 29 of 41 ids
"missing" this way, most of them not real defects, though the noise does
contain one genuine finding: `GDPR-1.0_req_art_32.1`, the umbrella clause
`golden-answers.md`'s own correction (2026-08-06) documents as the
recurring miss, is genuinely never mentioned in that answer at all — a real
signal, just buried in a lot of false alarm.

**Two real failure classes in this same re-run are entirely outside what
this check can see.** H11/`qwen3:14b`'s single `run_cypher` call returned
zero rows (a property/mapping miss, not a dropped citation) — there is no
evidence to check citation against when retrieval itself already failed,
so the check stays silent on a genuinely wrong answer. H1/`qwen3-coder-next`
correctly *names* `cap_cybersecurity_risk_management_program_50601b` and
`cap_security_control_effectiveness_assessment_627623` but asserts they're
"governed by approved policies" when `golden-answers.md` confirms both are
entirely ungoverned — a wrong claim about a cited id, which a
presence/absence check cannot catch by construction; it checks whether an
id was *mentioned*, not whether what's said about it is true.

**Call: don't adopt this as built, and it doesn't compete with union-of-N
even in a refined form.** The literal spec (last tool result, any tool
type, substring match) has real problems on all three axes tested here
(wrong scope of tool, wrong scope of trace, wrong matching strategy). A
corrected version — `run_cypher`-only, unioned across the full trace, with
some form of name/abbreviation resolution instead of raw substring
matching — has exactly one clean piece of positive evidence behind it
(H11/`qwen3-coder-next`) against union-of-N's 7/7 on its own test question,
and structurally can't reach two of the failure classes documented in this
same re-run (wrong retrieval, wrong claims about cited data). It's a
narrower, noisier tool aimed at a narrower slice of the problem — worth
revisiting later as a candidate cost-reduction heuristic (e.g., skip extra
union-of-N samples when a single run's citation set already looks complete)
once union-of-N's cost is a real problem worth optimizing against, not a
substitute for building it.

### What this changes about the recommendation

Of the four ideas tested, **plain union-of-N with mechanical (regex,
not LLM) extraction of cited ids is the only one with unambiguous positive
evidence** — cheap to combine, doesn't fix systematic bugs (if every
sample makes the same mistake, sampling more doesn't help), but does fix
the stochastic under-citing that's the main thing left after the grounding
fix. **Validator-as-detector is not yet reliable enough to trust
unsupervised** — it caught a real gap once and missed two equally real
ones afterward (a 4/7 and, in the schema-grounding controlled comparison,
a 5/7, neither flagged), which is a meaningfully different finding than
"the validator works" — it means any validator step needs its own accuracy
testing before being trusted as a gate, not just existence. And critically,
**the fix that resolved the generator's failures didn't transfer to the
validator's** — schema grounding was a knowledge gap fix; the validator's
gap looks like a completeness/long-context-attention one, a different
problem that happens to look similar because both involve "give the model
more context." **Combining synthesis with pooled evidence made things
worse, not better** — more raw material available didn't survive an extra
LLM judgment step. **Don't lower temperature** — still holds, data points the
opposite direction from the usual assumption.

## Next

`qwen3-coder-next:q4_K_M` (51GB, user-recommended for tool calling) was
tried on 2 of the 5 sampled questions (H1, H11) — both discussed above.
`qwen3.6:35b-mlx` remains downloaded but untested (time/cost budget for
this pass; each `qwen3-coder-next` call ran long enough that a fourth
model's full sample wasn't run). What was tried narrows the next step
usefully:

1. **Revised, after the grounding-location fix above: prompting/grounding
   got further than expected, so try that harder before reaching for
   parameterized templates.** The original version of this note argued the
   three Cypher-writing failure classes (wrong properties, wrong ID
   patterns, reversed direction) meant freehand Cypher had a floor no
   amount of prompt care would clear, and recommended constraining
   `run_cypher` to parameterized sub-queries. Moving the schema into the
   system prompt and fixing one misleading example then eliminated the
   ID-pattern and direction failures in both retests — evidence against
   that conclusion, not for it. The honest updated position: it's not yet
   known whether prompting further (e.g. explicit "verify each hop
   independently before trusting a multi-hop query's row count," lifted
   from `q-approach1.md`'s own FalkorDB reliability finding) closes the
   remaining gaps (H1 stopping early, H11 under-citing), or whether there's
   a real ceiling prompting can't reach. Parameterized templates remain the
   fallback if further grounding iterations stop paying off, not the
   immediate next step.
2. **Residual failure mode after the grounding fix**: stopping early / under-
   citing. H1 answered "here's the chain we'd need to check" instead of
   actually checking it after one tool call; H11 cited 3 of 7 real
   obligations in one run, 7/7 in another, 0/7 in a third — see "Further
   experiments" below, which tested three fixes for exactly this rather
   than proposing them speculatively. Union-of-N has real, evidenced payoff
   — **now wired into `query_mechanism_v2.py` itself and empirically
   re-verified, see `union-of-n.md`**, including a genuinely new finding
   that re-run surfaced: sampling 3x can turn a single model's reliable (if
   flawed) convergence into a 0-for-3 total failure on a demanding question,
   a real cost the standalone experiment's synthetic test didn't expose;
   a validator-as-detector layer is promising but its auto-revise loop needs
   a redesign (its own turn budget, not one shared with the original
   exploration) before it's worth adopting; lowering temperature made things
   worse, not better, and shouldn't be tried as a fix for this. A remaining
   untested idea from that pass: an explicit "don't answer with what you'd
   still need to check — either check it, or state plainly you're stopping
   short and why" prompt instruction, cheaper than any of the three tested
   ideas and not yet tried.
3. ~~Failure mode (b) from the original 14B results — correct data, wrong
   synthesis (H13's hallucinated count, H11 attempt 1's dropped rows) —
   likely needs a programmatic check (verify the final answer text
   references every distinct primary key from the last tool result) rather
   than a prompt instruction alone, the same "annotate structurally, don't
   leave it to prose" lesson `_annotate_trust()` already encodes for the
   trust flag in `query_mechanism_v1.py`.~~ Tested: see "Citation
   completeness as a deterministic post-check" above. One real catch, but
   the literal spec (last result, any tool, substring match) needed
   correcting on all three axes, still can't reach two of the failure
   classes documented in this same pass, and isn't a substitute for
   union-of-N's already-unambiguous evidence.
4. `MAX_AGENT_TURNS`'s default (8) may need less raising than the earlier
   16-turn failure suggested — `qwen3-coder-next` converged on H11 in 7
   turns once grounding was fixed, well under even the default. Worth
   re-checking whether turn-limit pressure is still a real constraint at
   all before adding the complexity of a per-model or progress-aware limit.
5. Run the full 13-question set (5 of 13 sampled here) and grade against
   every rubric in `golden-answers.md`, then do the cost/latency comparison
   against approach 1's free floor this doc originally scoped as the bar to
   clear.

## Scope for this pass

**Targeted** (the 13 questions needing semantic/agentic reasoning):
M3, M5, M14, H1, H3, H5, H6, H8, H9, H11, H12, H13, H14.

**Explicitly not targeted, flagged instead** (schema gaps, not mechanism
gaps): H10, H15.

## Test plan

Everything reachable without a live LLM gets a real, deterministic test in
`test_query_mechanism_v2.py`: the read-only-query guard (reject write
Cypher), `whole_graph_stats()`'s numbers against the same golden values
`test_query_mechanism_v1.py` already verifies, `list_entities()` against
live data, and the fallback-to-v1 path. The agent loop's plumbing — tool
results fed back correctly, the loop terminating, read-only enforcement
actually blocking a write — is exercised with a scripted `FakeLLMClient`
(22/22, no model needed). **Update: a real model is also now available**
(see "Environment constraint" below) — a sampled, honest (not exhaustive)
set of real runs against `qwen3:14b` via `OllamaClient` is in the "Result"
section further down, including two prompt-fix iterations. The full
13-question rubric grading pass this section originally deferred is still
not done — 5 of 13 were sampled, not all — and remains the next step.

## Files

- `query_mechanism_v2.py` — `LLMClient` protocol, `NoLLMConfigured` default,
  the three tools, the agent loop, and the fallback-to-v1 wrapper.
- `test_query_mechanism_v2.py` — tool-level tests against live data plus
  scripted-agent plumbing tests, per the test plan above.
- `experiment_citation_completeness.py` — standalone, not wired in; tests
  "Next" item 3's deterministic citation-completeness post-check, see the
  "Citation completeness as a deterministic post-check" section above.
