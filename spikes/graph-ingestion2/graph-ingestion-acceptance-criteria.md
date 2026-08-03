# Acceptance Criteria — Graph Correctness & Completeness Fix (v3)

Synthesized from `FIX_DESIGN.md`, `FIX_DESIGN_V2.md`, and iterative review in
this session, then reconciled against the actual source of truth,
[`docs/framework/policy-system-domain-concepts.md`](../../framework/policy-system-domain-concepts.md)
— the spike-local `data-model.md` that used to live in this folder was a
simplified, partially-drifted implementation artifact, not the canonical
model; its schema tables have since been merged into
`domain-concepts.md` and the file removed.

v1 proposed a digit-signature match to fix SATISFIED_BY without checking
`source_ref` against real data or against the domain model. Both checks
surfaced deeper issues: Requirement node identity had drifted from spec, and
— more significantly — Obligation identity is architecturally wrong for this
system's stated purpose. See AC-4.

## Implementation priority

**The ACs below are ordered to match their logical implementation
sequence — read top-to-bottom.** (Renumbered 2026-08-02; see git history if
you need the old AC-N ↔ new AC-N mapping.)

- **AC-1** (failure-counting instrumentation) comes first, not last.
  Every collapse found in the 2026-08-02 graph analysis (AC-2: 801/804
  Requirements orphaned; AC-3: 521/523 Obligations orphaned; AC-7: 0/146
  Roles ever linked) was invisible in the pipeline's own output — it
  printed clean-looking success messages while silently dropping >99% of
  edges. AC-1 turns that into a visible number every later fix can be
  checked against, instead of requiring a manual graph query to catch a
  regression.
- **AC-2 and AC-3** come next, together — same root-cause
  fallback-to-first collapse pattern, both CRITICAL/P0.
- **AC-4** (canonical identity) precedes **AC-5** (the chain-generation
  fix) because AC-5's count-invariant violation is partly explained by
  AC-4's finding that Capability IDs are regulation-prefixed — fixing
  identity first avoids redoing AC-5's work.
- **AC-6 precedes AC-7** — AC-6 (Role validity) is a stated precondition
  for AC-7 (HAS edge creation), so it must land first.
- **AC-8, AC-9** follow in either order relative to each other, after the
  edge/identity fixes they build on.
- **AC-10** is last: its reachability metric is only meaningful once
  AC-2/AC-3/AC-7's edges actually exist, and its "which parent" reporting
  needs AC-4/AC-5's identity fixes to be stable first.

## AC-0 — Original spike acceptance criteria (baseline)

Moved here from `README.md` 2026-08-02 — the spike's purpose is to learn,
and everything learned about what "done" actually requires belongs in this
document, not scattered across a README. These were the original,
structural success criteria; AC-1 through AC-10 below are what running the
spike against live data revealed was missing from them.

**The spike is successful when:**
1. Running `python run-spike.py --all` ingests both CRA and NIS2 regulations.
2. All 8 domain concepts are stored in FalkorDB: Regulation (with
   versioning), Role, Requirement, Obligation, Capability, Policy,
   Standard, Control.
3. All relationships are created: Regulation→CONTAINS→Requirement,
   Requirement→SATISFIED_BY→Obligation, Obligation→REQUIRES→Capability,
   Capability→GOVERNED_BY→Policy, Policy→SUPPORTED_BY→Standard,
   Standard→IMPLEMENTED_BY→Control.
4. Results are visible in FalkorDB (can query all nodes and relationships).
5. Operations are idempotent (safe to re-run).

**Superseded in detail, not in spirit, by AC-1–AC-10:** running the spike
does satisfy #1, #2, and #4 as literally stated — nodes and edges do get
created and are queryable. But #3 and #5 turned out to be necessary-not-
sufficient: edges "being created" is not the same as edges being *correct*
(AC-2, AC-3 show 99%+ of Requirement→Obligation and Obligation→Capability
edges are fallback-collapsed onto a handful of nodes), and "safe to re-run"
was never actually verified for relationships, only nodes (AC-9 shows
duplicate `CONTAINS` edges already exist after a single run). AC-0 stays
as the baseline definition of scope; AC-1–AC-10 are the correctness bar
within that scope.

## AC-1 — No fallbacks; every error and non-match must be surfaced, never masked

This is the cross-cutting rule every other AC's "leave unlinked and log"
language depends on. Stated once here as its own testable criterion instead
of relying on each AC to restate it correctly, because it's the single
principle that separates this fix from the two rejected `FIX_DESIGN*.md`
proposals. Sequenced first (see "Implementation priority" above) so every
later AC can be verified against a hard number instead of a manual query.

- [ ] No code path may substitute a default/first-available/arbitrary target
      when the real match fails — not for Requirement↔Obligation, Role↔
      Obligation, Obligation↔Capability, or Capability↔Policy↔Standard↔
      Control. A missing edge is an acceptable outcome; a fabricated one is
      not. (Restates AC-2/AC-3/AC-7's individual "no fallback" clauses as
      one binding rule, not three optional-looking suggestions.)
- [ ] Every non-match (no matching Requirement, no matching Capability, no
      matching canonical Obligation, unparseable `source_ref`, etc.) is
      logged with enough context to find the offending node afterward (its
      id and the field that failed to match) — not a bare "insert failed"
      message that scrolls past in stdout.
- [ ] Fixes the current anti-pattern in `run-spike.py`'s insertion loops:
      every insert is wrapped in `try/except Exception as e: print(...)` that
      swallows the failure and continues (e.g. lines 224-225, 240-241,
      260-261, 273-274, 286-287, 299-300, 312-313). These must either
      re-raise for failures that indicate a real bug (connection errors,
      malformed data), or — for expected non-matches — be counted and
      surfaced in the run summary, not just printed once and lost.
- [ ] The final `success_count` summary (`run-spike.py:434-448`) is extended
      to also report failure/unmatched counts per relationship type
      (Requirements with no Obligation match, Obligations with no Capability
      match, etc.), so a run that silently drops 90% of edges is visibly
      different from one that drops 2%. Currently only successes are
      counted — a pipeline that fails constantly and one that succeeds
      constantly produce equally clean-looking output.
- [ ] Verification: deliberately feed a chunk with a `source_ref` that cannot
      match any Requirement, and confirm the run reports it as an explicit
      unmatched/failed count rather than exiting with the same "ACCEPTANCE
      CRITERIA MET" message it would give on a clean run.

## AC-2 — Requirement identity + SATISFIED_BY (Requirement → Obligation) [CRITICAL / P0]

**Confirmed against live data (2026-08-02 run):** 801 of 804 Requirement
nodes (99.6%) have zero outgoing `SATISFIED_BY` edge. The 3 that do have
edges each absorb hundreds of unrelated Obligations — e.g.
`CRA_req_11_4d62f2` (`Article 1(1)(a)`) is linked to Obligations sourced
from `Article 31(1)`, `31(3)`, `31(4)`, and a hallucinated
`"Article 5 (implied from typical AI Act...)"` reference. This is the
`requirements[0]` positional-fallback anti-pattern from "Rejected
approaches" below, observed live — not a hypothetical risk.

- [ ] Requirement IDs are generated per the canonical scheme —
      `{REG}_req_art_{article_num}`, extracting only the leading
      `Article\s+(\d+)` number — not the current fragmented
      `{REG}_req_{all_digits}_{hash}` scheme that produces one node per
      extracted sentence (804 nodes currently, vs. ~100-120 real articles
      across CRA+NIS2). This is consistent with the real domain model's statement that "each
      requirement is expressed by exactly one regulation article/section"
      ([domain-concepts.md:194](../../framework/policy-system-domain-concepts.md)).
  - This makes Requirement node creation naturally idempotent via the
    existing `MERGE`-on-id insert (`graph.py:65-70`): multiple chunks/
    sentences referencing "Article 32" collapse onto one Requirement node,
    which is the intended granularity.
- [ ] Each Obligation links via `SATISFIED_BY` to the Requirement sharing its
      article number (same `Article\s+(\d+)` extraction applied to the
      Obligation's own `source_ref`) — not `requirements[0]`, not a
      full-digit signature (which collides across sub-provisions, e.g.
      `Article 1(1)(a)` through `(d)` all reduce to "11" under concatenation
      — confirmed against live CRA data).
- [ ] High fan-out (many Obligations per Requirement) is expected and is
      **not** by itself evidence of a bug — it only indicates many duties
      exist within one article. What must not happen is cross-article
      linkage within the same regulation.
- [ ] Accepted residual, not silently assumed away: some `source_ref` values
      are LLM-hallucinated (confirmed live examples reference "AI Act" and
      "Cybersecurity Skills Regulation" from within CRA extraction). An
      Obligation whose `source_ref` doesn't parse to a valid article number,
      or whose article has no corresponding Requirement, is left unlinked
      and logged — never defaulted onto an unrelated Requirement. AC-10
      adds an ingestion-time check (comparing the claimed `source_ref`
      against the actual `chunk_id` the text came from) that prevents most
      of this category outright; this bullet is the residual-handling
      safety net for whatever isn't caught there, not a substitute for it.
- [ ] Superseded by AC-4 for cross-*regulation* convergence: this AC governs
      matching *within* one regulation. Genuine cross-regulation reuse of the
      same underlying duty is AC-4's responsibility, made possible because
      AC-4 removes the regulation prefix from Obligation identity.

## AC-3 — REQUIRES (Obligation → Capability) [CRITICAL / P0]

**Confirmed against live data:** 521 of 523 Obligations (99.6%) have zero
outgoing `REQUIRES` edge. The 2 that do have edges absorb 228 and 154
Capability links respectively — the identical fallback-to-first collapse
seen in AC-2, on a different edge.

- [ ] **Collapse guard:** coverage (fraction of Obligations with ≥1
      outgoing `REQUIRES` edge) must exceed 5%, and no single Obligation
      may account for more than 25% of total `REQUIRES` edges. The
      2026-08-02 run shows 0.4% coverage with two nodes absorbing 228 and
      154 of 382 total edges (~100%) — a textbook fallback-to-first
      signature, not a sampling artifact.
- [ ] Primary match key: `capability['related_obligation_ref']`, already
      requested by the Pass-2 prompt (`extractor.py`) but never validated
      against real LLM output. **Before relying on it as primary**, sample
      actual `related_obligation_ref` values from a live extraction run to
      confirm the local Ollama model reliably returns a parseable reference
      rather than free-text paraphrase.
- [ ] `related_obligation_ref` must be persisted on the Capability node
      (`insert_capability_into_graph()` currently drops it, same gap AC-7
      fixes for `role_id` on Obligation) so match quality is inspectable.
- [ ] Fallback match (only if the primary reference is missing/unparseable
      or the sample check shows it's unreliable): capability name as a
      substring of obligation text.
- [ ] Final fallback: leave unlinked, log. Never default to "first available
      capability."

## AC-4 — Canonical, cross-regulation Obligation and Capability identity

**Context:** `domain-concepts.md:211` defines Obligation as "canonical,
reusable... across multiple regulations" with matching-before-minting
semantics — the same pattern `capability_taxonomy.py` already implements for
Capability (regulation-agnostic IDs like `cap_data_encryption`, keyword
matching via `find_matching_capabilities`). The current spike instead mints a
fresh, regulation-prefixed Obligation (`CRA_obl_*`, `NIS2_obl_*`) per
extraction with no cross-regulation matching, so the system's stated purpose
— recognizing GDPR's 72-hour breach notice and NIS2's 24-hour early-warning
requirement as the same underlying duty — is structurally impossible today,
not just incomplete.

**Correction to the above (2026-08-02):** this AC assumed Capability IDs
are already regulation-agnostic/canonical ("`cap_data_encryption`").
Confirmed false against both code and live data — `extractor.py:398` mints
`cap['id'] = f"{reg_prefix}_cap_{slug}"`, and the graph shows
`CRA_cap_cybersecurity_risk_manag`, `NIS2_cap_...`, etc. Capability has the
identical regulation-prefixed-identity flaw this AC sets out to fix for
Obligation. Fixing only Obligation would let obligations converge across
regulations while their Capabilities still don't — breaking the system's
stated purpose one layer up, just moved. (This is why the AC title now
names both concepts.)

- [ ] Widen this AC's scope: apply the same prefix-drop + taxonomy-based
      `find_matching_capabilities`-style matching to Capability ID
      generation (`extractor.py:398`) itself, not only to the new
      Obligation taxonomy. `capability_taxonomy.py` already has the
      taxonomy structure and matching function — the gap is that
      `extractor.py` isn't using it to *mint* the ID, only concatenating
      regulation prefix + slug directly.
- [ ] This connects to AC-5's count-invariant fix: once Capability IDs are
      canonical, `MERGE`-on-id will collapse duplicate CRA/NIS2
      capabilities before the Policy/Standard/Control transform runs,
      which may explain some of AC-5's observed Policy/Standard over-count.
- [ ] Obligation IDs drop the regulation prefix and are generated the same
      way Capability IDs already are (mirror `cap_{slug}` as `obl_{slug}`),
      making cross-regulation reuse possible via `MERGE`-on-id — the same
      mechanism that already dedupes nodes elsewhere in the pipeline.
- [ ] New `src/obligation_taxonomy.py`, mirroring `capability_taxonomy.py`'s
      structure exactly (`name`, `keywords`, `description` per canonical
      duty), pre-seeded with common cross-regulation duty patterns (e.g.
      "Report Security Incidents", "Conduct Cybersecurity Risk Assessment" —
      the domain doc's own examples).
- [ ] New `find_matching_obligation(text, existing_taxonomy)` — mirrors
      `find_matching_capabilities()` — checks each newly-extracted obligation
      candidate against the taxonomy by keyword before minting. Match → reuse
      the canonical Obligation id. No match → mint a new canonical
      (non-regulation-prefixed) Obligation and extend the taxonomy.
- [ ] SATISFIED_BY (AC-2) now connects a regulation/article-scoped
      Requirement to a canonical, potentially cross-regulation Obligation —
      so it becomes genuinely possible (and expected) for Requirements from
      *different* regulations to point at the same Obligation node. AC-2's
      "no cross-article leakage" rule still applies to matching *within* a
      regulation; this AC is what makes legitimate cross-*regulation*
      convergence possible on top of it.
- [ ] HAS (Role → Obligation) is treated as many-to-many in practice — one
      canonical Obligation may have `HAS` edges from multiple Roles across
      different regulations (per the domain doc's own worked example, line
      219). Flagging upstream, not silently resolving: `domain-concepts.md`
      states the opposite cardinality ("assigned to exactly one role") in
      the same paragraph as that example — internally inconsistent, and the
      doc should be corrected rather than the code silently picking a side.
- [ ] Scope boundary: semantic matching here is keyword-based (same fidelity
      as the existing Capability taxonomy), not true NLP/embedding
      similarity — duties phrased very differently across regulations may
      still mint as separate canonical Obligations. That's an accepted
      limitation of this spike, not a claim of full convergence.
- [ ] Larger in scope than the other individual-edge-type ACs in this
      document: this touches Obligation *and* Capability ID generation,
      adds a new taxonomy module, and changes the extraction pipeline's
      minting logic — not just `run-spike.py`'s edge-creation loop. Treat
      as a distinct implementation phase.

## AC-5 — GOVERNED_BY / SUPPORTED_BY / IMPLEMENTED_BY (Capability→Policy→Standard→Control)

- [ ] `transform_capabilities_to_policy_chain()` (`transformer.py:64-98`)
      generates exactly one Policy/Standard/Control per Capability in one
      loop — carry the parent reference forward explicitly
      (`policy['capability_id']`, `standard['policy_id']`,
      `control['standard_id']`) and use it for exact-lookup edge creation.
      No positional pairing, no fallback-to-first.
- [ ] The Standard→Control edge type must be renamed from the code's current
      `VALIDATES` to `IMPLEMENTED_BY`. This is not a judgment call — the real
      source of truth
      ([domain-concepts.md:271,401](../../framework/policy-system-domain-concepts.md))
      is explicit on `IMPLEMENTED_BY`; the code disagrees.
- [ ] Flag upstream, don't silently resolve: `domain-concepts.md` itself is
      internally inconsistent on Capability↔Policy cardinality — the
      Capability section (line 237) says "governed by exactly one policy"
      while the Policy section (line 253) says "governed by one or more
      policies." The code's 1:1 transform matches the stricter (Capability
      section) reading, which this AC keeps, but the doc should be
      corrected to stop contradicting itself.
- [ ] **Invariant, not just orphan-checking:** because generation is
      strictly one Policy per Capability, one Standard per Policy, one
      Control per Standard, `count(Policy) == count(Capability)`,
      `count(Standard) == count(Policy)`, and `count(Control) ==
      count(Standard)` must hold exactly. The 2026-08-02 run shows
      Capability=631 but Policy=Standard=677 and Control=687 — **more
      downstream nodes than Capabilities**, which is structurally
      impossible under a correct 1:1 loop and indicates either duplicate
      Policy/Standard/Control minting or an ID-generation bug independent
      of the orphan-edge issue below.
- [ ] **Orphan population, previously unaddressed:** this AC assumed zero
      orphans as a given; live data shows 249/631 (~39%) Capabilities with
      no `GOVERNED_BY`, 282/677 (~42%) Policies with no `SUPPORTED_BY`, and
      290/687 Controls with no incoming edge from a Standard. Root-cause
      before fixing: sample `capability_id` values on both sides of the
      transform to check for a slug-truncation or regulation-prefix
      mismatch (see AC-4's finding that Capability IDs are
      regulation-prefixed) causing lookups to fail even though generation
      ran.

## AC-6 — Role validity (precondition for AC-7)

- [ ] Only roles passing `validate_roles_by_obligation_subject()`
      (`extractor.py:139-219`, currently unused) are persisted as Role nodes.
- [ ] Target, not guarantee: Article-3-style defined terms (Software,
      Hardware, End-point, etc.) should be substantially reduced as Role
      nodes. The validation only checks grammatical subject-position before
      a duty verb, which doesn't fully distinguish actors from objects in
      passive-adjacent phrasing ("The product shall be designed to...") — a
      residual false-positive rate is expected, not eliminated.
- [ ] **Collapse guard:** same 5%-coverage / no-single-node-concentration
      rule as AC-7 applies here too, since AC-7's `HAS` collapse is
      downstream of role validity — a 0% Role-orphan improvement here with
      no corresponding rise in `HAS` coverage means the bug is elsewhere in
      the chain, not fixed by this AC alone.

## AC-7 — HAS (Role → Obligation)

- [ ] Every Obligation with a `role_id` produces one `HAS` edge via
      `create_role_has_obligation()` (`graph.py:417-433`, currently unused).
- [ ] `role_id` is persisted as an Obligation node property (currently
      dropped by `insert_obligation_into_graph()`).
- [ ] Known limitation, not silently assumed away: the existing role-match
      heuristic (`extractor.py:340-363`) assigns only the *first* matching
      role name per obligation and stops — obligations naming joint subjects
      ("Manufacturer, Importer, or Distributor shall...") will only link to
      one of them. Acceptable for this spike; not claimed as fully correct.
- [ ] See AC-4: once Obligation becomes canonical/cross-regulation, a single
      Obligation may legitimately accumulate `HAS` edges from Roles in
      *different* regulations (e.g. GDPR's Data Controller and NIS2's
      Operator of Essential Services both `HAS` "Report Security Incidents").
      This is expected fan-in, not a bug, once AC-4 is implemented.
- [ ] **Collapse guard:** match rate must not be near-zero. Concretely: the
      fraction of Roles with ≥1 outgoing `HAS` edge must exceed 5% — a 0%
      rate, as observed live in the 2026-08-02 run (0 of 146 Roles, 0 `HAS`
      edges graph-wide), is presumptively a matching bug, not a data
      limitation. Report this coverage number in the run summary (ties
      into AC-1).
- [ ] `role_id` must be verified non-null on the in-memory Obligation dict
      *before* the HAS-edge-creation loop runs (`run-spike.py:277`), not
      only checked as a persisted graph property — the 2026-08-02 run
      showed 0/523 Obligations with a role_id anywhere, meaning the
      extractor's matching heuristic itself is failing on real text, not
      just the persistence gap this AC originally described.

## AC-8 — Obligation ID collisions (both directions)

- [ ] **Over-generation:** duplicate nodes for the same real obligation
      across re-runs. Root cause not yet confirmed against real duplicate
      pairs in the live graph — sample a handful of the 75 duplicated-text
      groups before assuming whitespace/casing drift is the cause; LLM
      rewording (no temperature/seed control in `_call_llm`) may be the real
      driver, which normalization won't fix.
- [ ] **Under-generation / data loss:** the current ID scheme hashes only the
      first 50 characters of obligation text at 6 hex chars
      (`extractor.py:343`). Formulaic regulatory openers ("The manufacturer
      shall ensure that...") make it plausible for two genuinely different
      obligations to collide onto the same ID and silently merge via
      `MERGE`, losing one. Fix: hash normalized *full* text (lowercased,
      whitespace-collapsed) at a longer digest, not a 50-char prefix.
- [ ] Interacts with AC-4: once Obligation identity is taxonomy/keyword-based
      rather than text-hash-based, this AC's collision risk shrinks
      naturally for obligations that match an existing canonical entry —
      it still applies to newly-minted canonical Obligations that don't
      match anything in the taxonomy.

## AC-9 — Relationship idempotency (safe to re-run, edges included)

**Context:** `README.md`'s own acceptance criterion (AC-0, item 5:
"idempotent operations, safe to re-run") is currently verified only for
node creation (via `MERGE`-on-id). No AC checks relationship creation.
Live evidence: 993 `CONTAINS` edges exist between Regulation and
Requirement nodes, but there are only 804 Requirement nodes — i.e.
duplicate `CONTAINS` edges between the same node pairs already exist after
a single run.

- [ ] All relationship-creation calls (`graph.py`'s edge-creation
      functions) must use `MERGE` on `(source)-[:REL_TYPE]->(target)`, not
      `CREATE`, so re-running the pipeline (or re-processing overlapping
      chunks within one run) cannot inflate edge counts.
- [ ] Verification: `MATCH ()-[r:CONTAINS]->() RETURN count(r)` must equal
      `MATCH (n:Requirement) RETURN count(n)` (each Requirement belongs to
      exactly one Regulation, per the domain model) — currently 993 vs.
      804, a confirmed live discrepancy.
- [ ] General idempotency test: run the pipeline twice on identical input;
      every edge-type count must be unchanged between runs.

## AC-10 — Provenance traceability back to source regulatory text

**Context:** only Requirement and Obligation nodes currently carry a
`source_ref` property, and even that is an unvalidated LLM claim, not a
verified pointer to the chunk it was actually extracted from (see AC-2's
hallucinated-reference finding). Role and Capability nodes have **zero**
provenance property despite the extraction prompts already asking the LLM
for one (`extractor.py:45` for roles, `extractor.py:100` for
`related_obligation_ref` on capabilities) — `insert_role_into_graph`
(`graph.py:176-200`) and `insert_capability_into_graph` silently drop them
before persisting. Policy/Standard/Control carry no parent-id reference
either (see AC-5), so they cannot be traced back to anything at all.

Per the corrected domain model (see
[domain-concepts.md](../../framework/policy-system-domain-concepts.md)'s
new "Provenance & Traceability" principle), only Regulation, Role, and
Requirement are extracted directly from regulatory text and should carry a
direct source reference; Obligation, Capability, Policy, Standard, and
Control are canonical/generated concepts whose provenance is established
**transitively**, by walking compliance-chain edges back to a
Requirement's `source_ref` — not by duplicating a source_ref onto every
node. That reframes edge completeness (AC-2, AC-3, AC-5, AC-7) as a
provenance-integrity requirement, not just a relationship-completeness one:
an Obligation with no `SATISFIED_BY` edge isn't just "missing a link," it
is **unprovenanced** — nothing in the graph can show which regulatory text
justifies its existence.

- [ ] `insert_role_into_graph` persists `source_ref` (already produced by
      the extractor, already documented in the function's own docstring,
      just never written to the props dict).
- [ ] `insert_capability_into_graph` persists `related_obligation_ref`
      (same gap AC-3 already flags for match-quality purposes — this AC
      adds the traceability angle: without it, a Capability with a broken
      `REQUIRES` edge has no fallback path back to source text at all).
- [ ] **Ingestion-time verification, not just structured format — the
      objective is zero invented references, not merely well-formed ones.**
      `source_ref` being structured/parseable (regulation id +
      article/section number) is necessary but not sufficient: a
      well-formatted reference can still be fabricated (`"CRA Article
      31(1)"` is valid syntax even if the LLM never saw Article 31 for
      that extraction call). The actual check: `chunker.py` already splits
      source HTML by article before the LLM ever sees it, so the pipeline
      knows with certainty which single article's text was fed into each
      extraction call (`chunk_id`/`article_id`). At ingestion time,
      compare every extracted item's claimed `source_ref` article number
      against the `chunk_id` of the chunk that produced it — zero
      additional LLM calls, pure string comparison against ground truth
      the pipeline already has. Any mismatch (different article, different
      regulation) is definitionally invented, since it wasn't in the text
      given to the model for that call — reject it (log per AC-1, do not
      persist as an unflagged `source_ref`). This alone would have caught
      100% of the observed "AI Act" / "Cybersecurity Skills Regulation"
      hallucinations, at zero marginal cost, at ingestion time instead of
      downstream during graph analysis.
- [ ] Accepted limitation, not silently assumed away: this check only
      catches cross-article/cross-regulation hallucination (the case
      actually observed live). It does **not** catch a subtler case — the
      LLM inventing a sub-paragraph *within* the correct article (e.g.
      claiming `"Article 1(1)(e)"` when the real text only goes to `(d)`)
      — which would require a second-tier check verifying the extracted
      `text` is actually grounded in the chunk's raw content, not just
      plausibly near it. Out of scope for this spike; AC-2's "leave
      unlinked and log" residual-handling is the safety net for whatever
      slips past this check.
- [ ] New verification metric, reported in the run summary (ties into
      AC-1): for each non-Requirement node type, the percentage reachable
      to at least one Requirement via outbound compliance-chain edges
      (Obligation → SATISFIED_BY⁻¹, Capability → REQUIRES⁻¹ →
      SATISFIED_BY⁻¹, etc.). Given AC-2/AC-3/AC-7's live findings (99%+
      orphan rates), this number is currently near 0% for Obligation and
      Capability — that is the real, user-facing cost of the edge
      collapses documented elsewhere in this doc, stated in provenance
      terms rather than raw edge counts.

## Rejected approaches (do not reintroduce)

- Fallback-to-first-available-X for any relationship — see AC-1, which makes
  this a hard rule rather than a per-AC suggestion.
- Positional/index-based pairing as a primary linking mechanism.
- Treating 100% edge coverage as the success metric instead of "every edge
  that exists is real."
- Swallowing errors in a `try/except: print()` without aggregating them into
  a visible failure count (see AC-1).
- Assuming a "deterministic 1:1 generation" loop is orphan-free without
  checking node-count invariants (see AC-5).
- Treating a sibling concept (e.g. Capability) as an already-solved
  reference model without re-verifying it against live IDs (see AC-4).

## Verification checklist

```cypher
-- AC-1: no direct graph query — verified via the run summary reporting
-- failure/unmatched counts per relationship type, not graph state.

-- AC-2: Requirement count should approximate real article count (~100-120
-- across CRA+NIS2), not scale with extracted-sentence count
MATCH (r:Requirement) RETURN count(r);

-- AC-2: fan-out is fine within an article, flag cross-article leakage by
-- spot-checking a sample of edges, not just counting
MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation)
RETURN req.id, req.source_ref, o.source_ref LIMIT 20;

-- AC-3: resolution rate via related_obligation_ref specifically (sample,
-- don't just count edges), plus the collapse guard itself
MATCH (c:Capability)-[:REQUIRES]-() RETURN c.related_obligation_ref LIMIT 20;
MATCH (o:Obligation) WHERE NOT (o)-[:REQUIRES]->() RETURN count(o);
MATCH (o:Obligation)-[:REQUIRES]->(c) RETURN o.id, count(c) as n
ORDER BY n DESC LIMIT 5;

-- AC-4: cross-regulation convergence should now be observable — at least
-- some canonical Obligations should have HAS edges from Roles whose id
-- prefixes differ (CRA_role_* vs NIS2_role_*)
MATCH (r:Role)-[:HAS]->(o:Obligation)<-[:HAS]-(r2:Role)
WHERE r.id STARTS WITH 'CRA' AND r2.id STARTS WITH 'NIS2'
RETURN o.id, r.id, r2.id LIMIT 10;

-- AC-4 (widened): Capability IDs must NOT be regulation-prefixed once
-- fixed — this should return 0
MATCH (c:Capability) WHERE c.id STARTS WITH 'CRA_' OR c.id STARTS WITH 'NIS2_'
RETURN count(c);

-- AC-5: zero orphans expected here (deterministic 1:1 generation)
MATCH (c:Capability) WHERE NOT (c)-[:GOVERNED_BY]->() RETURN count(c);
MATCH (p:Policy) WHERE NOT (p)-[:SUPPORTED_BY]->() RETURN count(p);
MATCH (s:Standard) WHERE NOT (s)-[:IMPLEMENTED_BY]->() RETURN count(s);

-- AC-5: count invariant — these four must each return equal numbers
-- (Policy/Standard/Control count must never exceed Capability count)
MATCH (c:Capability) RETURN count(c);
MATCH (p:Policy) RETURN count(p);
MATCH (s:Standard) RETURN count(s);
MATCH (ctrl:Control) RETURN count(ctrl);

-- AC-6/7: Role coverage rate (collapse guard: must exceed 5%, i.e. this
-- count must be well below total Role count, not equal to it)
MATCH (n:Role) WHERE NOT (n)--() RETURN count(n);
MATCH ()-[r:HAS]->() RETURN count(r);

-- AC-8: both directions — duplicate texts under different ids, AND
-- suspiciously short/generic text at the same id (sample manually)
MATCH (o:Obligation) WITH o.text AS t, count(o) AS n WHERE n > 1
RETURN count(t) AS duplicated_texts, sum(n) AS total_dupe_nodes;

-- AC-9: relationship idempotency — CONTAINS count must equal Requirement
-- count (currently 993 vs. 804, a confirmed live discrepancy)
MATCH ()-[r:CONTAINS]->() RETURN count(r);
MATCH (n:Requirement) RETURN count(n);

-- AC-10: ingestion-time reference check can't be verified by graph query
-- alone (a correctly rejected item never reaches the graph). Verify via
-- pipeline unit test instead: feed a chunk for "Article 5" plus an
-- obligation dict claiming source_ref "Article 31" and confirm it is
-- rejected/logged rather than inserted.

-- AC-10: Role/Capability provenance fields must be non-empty once fixed
MATCH (r:Role) WHERE r.source_ref IS NULL OR r.source_ref = '' RETURN count(r);
MATCH (c:Capability) WHERE c.related_obligation_ref IS NULL
  OR c.related_obligation_ref = '' RETURN count(c);

-- AC-10: reachability — Obligations/Capabilities with NO path back to any
-- Requirement are provenance-broken, not just edge-incomplete
MATCH (o:Obligation) WHERE NOT ()-[:SATISFIED_BY]->(o) RETURN count(o);
MATCH (c:Capability) WHERE NOT ()-[:SATISFIED_BY]->(:Obligation)-[:REQUIRES]->(c)
RETURN count(c);
```
