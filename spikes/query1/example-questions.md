# Example Questions

Catalog of realistic questions the [target audiences](../../readme.md#target-audiences)
would ask the Query & Check layer, organized by *graph-traversal difficulty*
rather than by audience — the same question shape (a 1-hop lookup, a
multi-hop cross-regulation join, a gap analysis) recurs across audiences,
and difficulty is what actually drives the chat architecture decision.

Each question is tagged:

- **Audience** — who'd plausibly ask it (see readme table)
- **Graph pattern** — the traversal it requires, in domain terms (see [`ps-domain-concepts.md`](../../docs/artifacts/ps-domain-concepts.md))
- **Status today** — whether the current graph can actually answer it:
  - ✅ **answerable** — data exists, it's a structural/aggregation query
  - 🟡 **partial** — answerable but with caveats (unmerged capabilities, toy-only data)
  - ⛔ **blocked** — the data this needs doesn't exist yet
  - 🧠 **needs judgment** — not a retrieval problem; needs interpretation beyond graph traversal
- **Grading** — how a candidate answer gets verified once we test query mechanisms against it:
  - **exact-match** — one correct scalar/path value, computed directly from the graph
  - **set-match** — a correct *set* of ids/names (order-independent), computed directly from the graph
  - **rubric** — no single correct answer; graded against a written rubric (criteria a good answer must hit — correct entities cited, correct provenance, appropriate hedging), not string equality. Needed wherever the question requires interpretation (similarity judgments, situational compliance calls, hypotheticals) rather than pure retrieval.

For exact-match/set-match questions answerable today (✅), the golden value can
be computed now by running the equivalent Cypher directly. For 🟡/⛔ questions,
grading *method* is fixed here, but the golden value itself waits on the
synthetic data. Rubrics are drafted once we know what the underlying data
actually contains, so they can name real entities instead of staying abstract.

**Golden values and rubrics for every question are now computed** — see
[`golden-answers.md`](./golden-answers.md). Computing them surfaced two kinds
of catalog drift worth knowing about: three questions (S2, S4, S6, and by
extension M2) referenced illustrative entity names written before extraction
that don't exist in the real data, now fixed below; and several status tags
(S7/S8, M7, H1/H2/H4/H5/H6/H7) were stale from before the Helvex spike loaded
real Policy/Standard/Control data — this pass corrects them.

---

## Simple — single entity lookup, 1-hop traversal

Direct property reads or one edge-hop. No aggregation, no cross-regulation
reasoning. This tier is where a thin NL→Cypher translator should be enough.

| # | Question | Audience | Graph pattern | Status | Grading |
|---|---|---|---|---|---|
| S1 | "What roles does GDPR define?" | Legal Counsel, Compliance Officer | `Regulation{id:GDPR-*} -DEFINES-> Role` | ✅ | set-match |
| S2 | "What's the text of CRA Article 13.1?" | Legal Counsel | `Regulation -EXPRESSES-> Requirement` property read | ✅ (corrected from "Article 11" — out of CRA's extraction scope, see `golden-answers.md`) | exact-match |
| S3 | "What obligations does the Manufacturer role carry under CRA?" | Compliance Officer | `Role{name:Manufacturer} -HAS-> Obligation` | ✅ | set-match |
| S4 | "What capabilities does 'Maintain Security Logging' require?" | Security Architect | `Obligation -REQUIRES-> Capability` | ✅ (corrected from "Maintain Security Monitoring" — no such obligation exists, see `golden-answers.md`) | set-match |
| S5 | "When does CRA become effective, and what's its current status?" | Compliance Officer | `Regulation` property read | ✅ | exact-match |
| S6 | "Which requirement does the 'Maintain Security Logging' obligation satisfy?" | Auditor | `Obligation <-SATISFIED_BY- Requirement` (inbound) | ✅ (corrected from "Maintain Structured Access Logging" — no such obligation exists, see `golden-answers.md`) | exact-match |
| S7 | "What policy governs the 'Security Logging' capability?" | Policy Manager | `Capability -GOVERNED_BY-> Policy` | ✅ real data (Helvex spike loaded the Policy layer over real capabilities) | exact-match |
| S8 | "List the standards under the Data Protection Policy." | Policy Manager | `Policy -SUPPORTED_BY-> Standard` | ✅ real data | set-match |

---

## Medium — multi-hop traversal, aggregation, cross-regulation joins

Requires walking 2+ edges, grouping/counting, or joining across regulations
through a shared node. This is where the domain model's actual value
proposition (convergence, traceability) starts to show up in answers — and
where a naive single-shot Cypher translation starts to strain.

| # | Question | Audience | Graph pattern | Status | Grading |
|---|---|---|---|---|---|
| M1 | "Which capabilities are required by more than one obligation?" | Risk Manager | `Capability <-REQUIRES- Obligation`, `count() > 1` | ✅ | set-match |
| M2 | "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires." | Auditor, Security Architect | `Requirement -SATISFIED_BY-> Obligation -REQUIRES-> Capability`, chained | ✅ (corrected from "Art. 11" — out of CRA's extraction scope, see `golden-answers.md`) | exact-match |
| M3 | "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?" | Security Architect | Cross-regulation join *through* Capability | 🟡 re-verified: ran `find_capability_duplicates.py` against the live 68-capability graph — 0 candidate pairs at the default threshold, and manual review down to threshold 0.15 (57 pairs) found no genuine duplicates left to merge. That's by design (`nis2-extraction-methodology.md` / `gdpr-extraction-methodology.md`: real cross-regulation overlaps were converged onto shared capability ids *at extraction time*, not left for the merge workflow). `cap_security_logging_c4d9e2` is only sourced from CRA-1.0 and HELVEX-SOP-1.0 — NIS2 and GDPR were never extracted with a distinct logging capability to converge, so this is an extraction-scope gap, not an unmerged-duplicates gap | rubric — "all three regulations" can't be a true set-match until NIS2/GDPR extraction is revisited for a logging-shaped obligation, if one exists in the source text |
| M4 | "How many obligations does GDPR place on Data Processors vs. Data Controllers?" | Risk Manager, Legal Counsel | `Role -HAS-> Obligation`, grouped by Role, filtered to GDPR | ✅ | exact-match |
| M5 | "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?" | Legal Counsel | Cross-regulation Role comparison | 🧠 Role is *not* canonical by design (only Obligation/Capability converge) — needs semantic similarity over Role name/description, not a structural join | rubric |
| M6 | "Which obligations are backed by the weakest extraction confidence, and should be reviewed?" | Compliance Officer | `Obligation.confidence` threshold filter | ✅ | set-match against a fixed threshold (threshold choice itself is a rubric call) |
| M7 | "Show every path from a GDPR requirement down to a Control that verifies it." | Auditor | Full chain `Requirement→Obligation→Capability→Policy→Standard→Control` | 🟡 **status corrected** — this claimed ⛔ ("chain breaks after Capability") was stale; the Helvex spike wired Policy/Standard/Control over *real* CRA/NIS2/GDPR capabilities, not just Helvex-specific ones. 57 full GDPR chains exist today (confirmed live, see `golden-answers.md`), 31 current-evidence / 26 stale (16 through a `deprecated` Policy, 10 through a `draft`/`planned` branch) — prototyped in `query_mechanism_v1.py` | set-match (was exact-match "refusal") — the honesty test moves from "don't hallucinate a chain" to "don't present a stale/deprecated chain as current evidence" |
| M8 | "Which capabilities does our internal Helvex SOP regulation share with CRA?" | Security Architect | `Regulation{source_type:internal}` obligations converging on same `Capability` as `Regulation{source_type:external}` | ✅ real data confirmed live: `HELVEX-SOP-1.0`'s "maintain access logging for clinical trial systems" obligation and CRA's "maintain security logging" obligation both resolve to `cap_security_logging_c4d9e2` — no merge needed, convergence was authored directly (see `synthetic-data-spec.md`) | set-match |

---

## Hard — gap analysis, situational reasoning, data we don't have yet

These are the questions that actually matter to the business ("are we
compliant?") but either need the unpopulated Policy/Standard/Control layer,
need to map free-text about a real system into the graph's vocabulary, or
need external state (audit evidence, dates, system architecture) the graph
was never meant to hold.

| # | Question | Audience | What it needs | Status | Grading |
|---|---|---|---|---|---|
| H1 | "Are we compliant with GDPR Article 32?" | Risk Manager, Compliance Officer | Real obligation → real Policy → Standard → Control chain, plus Control pass/fail evidence | 🟡 **status corrected** — real per-sub-clause chains now exist (see `golden-answers.md`): 32.1a/b clean, 32.1c partial (one control still `planned`), 32.4 stale (governed only by a `deprecated` Policy) | rubric (compliant/partial/non-compliant + correct citation of the chain used to decide) — golden reasoning drafted |
| H2 | "Which capabilities required by CRA have no governing Policy yet?" (coverage gap analysis) | Risk Manager | `Capability` with no outbound `GOVERNED_BY`, scoped to CRA-derived capabilities | ✅ real data confirmed live: 55 of 68 capabilities ungoverned (13 governed) | set-match — golden value computed in `golden-answers.md` |
| H3 | "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?" | DevOps/Engineering | Map a free-text system description → Capability(s) it touches → check Controls | 🧠 needs NL-to-Capability mapping, but the Control layer it needs now exists (see H1) | rubric — golden reasoning drafted in `golden-answers.md`, grounded in H1's real chain |
| H4 | "Show me the audit evidence that our log retention control passed last quarter." | Auditor | `Control.evidence_ref` resolved against an external evidence store | ✅ real data confirmed live: `evidence://ci/log-retention-check/latest` | exact-match — correct answer is the `evidence_ref` pointer plus an explicit "the evidence store itself is out of scope," not a fabricated pass/fail |
| H5 | "NIS2 was updated — which of our Policies are now potentially out of date?" | Policy Manager | `Regulation -SUPERSEDED_BY-> Regulation` version diff, joined to real Policy layer | 🟡 **status corrected** — a real supersession now exists (`HELVEX-SOP-1.0 -SUPERSEDED_BY-> HELVEX-SOP-2.0`), plus a real `deprecated`/`draft` Policy pair to reason about staleness with; the NIS2-specific premise is still hypothetical | rubric — golden reasoning drafted |
| H6 | "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?" | Security Architect | Hypothetical capability insertion + redundancy detection across unmerged capabilities | 🧠 **premise correction** — an SBOM capability already exists (`cap_component_inventory_sbom_management_b5223c`, CRA-only); the real "hypothetical" is NIS2/GDPR convergence onto it, not minting a new node | rubric — golden reasoning drafted (today's correct answer: zero redundant coverage, no NIS2/GDPR obligation requires it yet) |
| H7 | "Which of our automated controls are due for review in the next 30 days?" | Auditor, Risk Manager | `Control.next_review_date` date-range filter | ✅ real data confirmed live: exactly 2 controls (`2026-08-15`, `2026-08-25`), anchored to the fixture's `2026-08-01` reference date | set-match — golden value computed in `golden-answers.md` |

---

## What this means for the prototype

- **Simple tier** validates that NL→Cypher (or a small fixed set of
  parameterized query templates) is sufficient and gets us a working demo
  fast, entirely on real data.
- **Medium tier** is where the domain model's actual pitch (cross-regulation
  convergence) has to show up to be credible. M8 is resolved: real Helvex
  data converges on `cap_security_logging_c4d9e2` alongside CRA, confirmed
  live, no merge step needed. M3 turned out *not* to be a merge-curation
  problem — `find_capability_duplicates.py` was run against the full
  68-capability graph and found zero genuine duplicates (down to threshold
  0.15, manually reviewed); every real cross-regulation overlap was already
  converged onto a shared capability id at extraction time (see
  `nis2-extraction-methodology.md` / `gdpr-extraction-methodology.md`), so
  `capability_merges.json` correctly stays empty. M3 is still blocked, but
  by an extraction-scope gap (NIS2/GDPR were never extracted with a
  distinct "Security Logging" capability to converge) rather than an
  unrun-merge one — revisiting the NIS2/GDPR extraction for a logging-shaped
  obligation is the actual next step if M3 needs to be a true three-way
  set-match.
- **Hard tier status was stale** — this pass found H2/H4/H7 fully ✅
  answerable and H1/H5 have real (if partial/stale-flagged) chains to reason
  over, all left over from before the Helvex spike's Policy/Standard/Control
  layer landed. Only H3 (needs live NL-to-Capability mapping, not just a
  golden chain to point at) and H6 (needs the redundancy-detection reasoning,
  not just the corrected premise) still require actual query-mechanism work
  rather than a documentation fix. The interesting chat-UX problem shifts
  from "the graph doesn't have this data yet" to a harder one: **surfacing
  chains that exist but are stale** — H1's deprecated-Policy branch and M7's
  14 chains through it are real, retrievable answers that would still
  mislead a user if presented without the staleness caveat. That distinction
  (structurally missing vs. present-but-untrustworthy) is now the real
  chat-UX decision, not "no matches found" vs. "not blocked."
