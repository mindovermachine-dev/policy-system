<!-- © 2026 Cartman ApS. All rights reserved. -->
# External Research Notes — Answer Verification / Failure-Mode Taxonomy

**Purpose:** this project's failure-kind taxonomy (see `README.md`) was derived
inductively from 33 failures across three of our own graded runs. This file
records what a web search of the broader RAG/QA-evaluation literature found,
so future work on this spike doesn't have to re-derive or re-search it.
Compiled 2026-08-09.

## Caution: one source in this research pass was likely fabricated, not extracted

`WebFetch` on the "Systematic Taxonomy of Failure Modes in RAG Systems"
(Garani, ACL Anthology TrustNLP 2026) PDF returned a fully-detailed, cleanly
formatted 33-item taxonomy on the first attempt. A second, independent
`WebFetch` attempt on a *different* paper's PDF, and a retry of this same
PDF, both returned an honest "this is binary/compressed content I can't
parse" response instead. Two honest failures and one suspiciously confident
success on similar binary content is a strong tell that the first response
was generated from general priors about "common ML pipeline failure
categories" rather than genuinely extracted from the paper — the category
names (Data Poisoning, Prompt Injection, Token Limit Exceeded, Latency
Issues, Version Incompatibility) read like a generic AI-systems-reliability
checklist, not the output of a paper the search results described as giving
each of 33 modes "a formal definition, observable manifestation, and
three-level evidence grading." **That detailed list was not used in the
analysis below.** Only the paper's abstract-level claims (accessed via the
HTML abstract page, which behaved honestly and declined to enumerate
specifics) are treated as reliable: 33 failure modes across 7 pipeline
stages (ingestion, representation, retrieval, generation, evaluation,
deployment, agentic orchestration), built from 48 sources, with
representation/evaluation/agentic-orchestration failures flagged as
comparatively under-researched despite frequent production occurrence. If
this paper's details matter later, re-fetch and re-verify — don't trust
this note's characterization of its content beyond what's stated here.

## Findings mapped to our taxonomy

### Convergent validation (independently derived, matches established research)

- **Completeness** (our largest class, 16/33) matches "Incomplete Answer,"
  one of exactly seven canonical failure points in Barnett et al.'s "Seven
  Failure Points When Engineering a RAG System" (CAIN 2024) — a
  three-case-study production study, independent of this project.
- **Granularity-slip** matches Barnett's "Incorrect Specificity" (answer
  too broad or too narrow relative to what was asked).
- **Miscount** matches a named category ("misstated numbers," "time/duration
  errors") in the hallucination-taxonomy literature's orientation/category/
  degree framework (Huang et al. survey).
- **The ensemble-judge decision (Stage 4)** is directly supported: LLM-judge
  reliability research documents position bias, verbosity bias,
  self-enhancement bias, and non-determinism in single-pass judging, and
  reports a three-judge consensus baseline reaching 97-98% macro F1 — a
  concrete precedent for "n≥3, not single-shot."
- **AD-2's deterministic/judgment split is the right axis.** The
  hallucination literature distinguishes *faithfulness* (consistency with
  given context) from *factuality* (truth independent of source). Our
  system only needs faithfulness — we ground to a fixed graph, not
  open-world knowledge — which is exactly AD-2's existing scope, not an
  arbitrary restriction.

### Folded into AD-7 / README this session

- **Barnett's "Missing Top Ranked Documents"** (relevant data exists,
  search/filter is under-scoped and doesn't surface it) is the precise
  shape behind SWE-M1 and CO-M2, both already in our Query-construction
  kind — this sharpens what that kind means rather than adding a new one.
- **Barnett's "Not in Context"** (truncation/consolidation loses retrieved
  data before generation sees it) has **no analog in our architecture** —
  our CLI returns direct, structured JSON from deterministic queries, not
  ranked chunks assembled into a context window. Worth stating explicitly
  as a structural advantage of AD-3's deterministic surface over generic
  vector RAG, not a gap to fix.
- **"Wrong Format"** (Barnett's seventh point — correct substance,
  unusable structure) has no analog in our current taxonomy at all. Added
  as an explicit Stage 4 rule-check target and a watch-item for Stage 5
  audits, even with zero confirmed instances in our 33 so far.
- **Barnett's "Not Extracted"** (data is present in what was retrieved, but
  generation fails to pull it out correctly, often due to noise/ambiguity
  in a large or contradictory result set) is closer to convergent
  validation of our existing **Completeness** kind than a new one — but
  the root cause differs from our confirmed Completeness instances
  (SA-H2/PM-H1/PM-H2/RM-H2 read as drafting omissions across otherwise-
  correct multi-call sequences, not confusion from noisy context). Flagged
  as a specifically-named Stage 5 watch-item: a large/noisy single result
  set could trigger this even without our system's usual multi-call
  decomposition pattern.

### Flagged, not yet decided (real design tradeoffs, deferred)

- **RAGAS's Faithfulness metric is a ratio** (claims-supported /
  total-claims), not our current binary scope-match pass/fail. A
  continuous score would capture over-claiming as *partial* faithfulness
  more precisely, but a threshold has to go somewhere either way — an open
  design choice, not adopted yet.
- **RAGAS separates Context Recall** (did retrieval surface everything
  relevant) **from Faithfulness** (did generation stay true to what was
  retrieved). We currently fold both into "Completeness." Splitting them
  would let Stage 5 distinguish "retrieval under-fetched" from
  "composition dropped what was fetched" — different root causes, likely
  different fixes. Not yet split.
- **Severity/degree grading** (mild/moderate/alarming, from the
  hallucination-taxonomy literature) isn't something we have — every
  failure is currently binary pass/fail. Could feed Stage 5's
  risk-weighted sampling as an additional axis alongside alias/near-match
  and comparison-shaped weighting. Not yet adopted.

### Concrete risk confirmed for our judge design specifically

LLM-judge research documents **"authority bias"** — judges favor answers
containing citations *even when the citation is fabricated*. Our whole
system is citation/ID-heavy, so this is a direct, literature-backed reason
citation/ID validity must never be delegated to the judge stage — it stays
a deterministic existence-grounding check. (The design already does this;
this is external confirmation it's the right call, not a new requirement.)

### No external validation found

Our **"Definitional/boundary — interaction between two defined terms"**
kind (SEC-M2/SEC-M4's overdue/deprecated composition-rule case) didn't
match anything found in this pass. Could be a genuine finding specific to
compliance/regulatory domains with precise, adjacent-but-distinct status
vocabularies — or literature exists that this search didn't surface. Left
as genuinely uncertain, not claimed either way.

## Sources

- [Seven Failure Points When Engineering a Retrieval Augmented Generation System (Barnett et al., 2024)](https://arxiv.org/abs/2401.05856)
- [Seven RAG Pitfalls and How to Solve Them — Label Studio summary](https://labelstud.io/blog/seven-ways-your-rag-system-could-be-failing-and-how-to-fix-them/)
- [A Survey on Hallucination in Large Language Models: Principles, Taxonomy, Challenges, and Open Questions](https://arxiv.org/pdf/2311.05232)
- [NVIDIA Metrics — Ragas documentation](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/nvidia_metrics/)
- [LLMs-as-Judges: A Comprehensive Survey on LLM-based Evaluation Methods](https://arxiv.org/pdf/2412.05579)
- [A Systematic Taxonomy of Failure Modes in Retrieval-Augmented Generation Systems (Garani) — abstract-level claims only, detailed content unverified/likely fabricated on fetch, see caution above](https://aclanthology.org/2026.trustnlp-main.27/)
