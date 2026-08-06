# Example Questions

Catalog of realistic questions the [target audiences](../../readme.md#target-audiences)
would ask the Query & Check layer, organized by *graph-traversal difficulty*
rather than by audience — the same question shape (a 1-hop lookup, a
multi-hop cross-regulation join, a gap analysis) recurs across audiences,
and difficulty is what actually drives the chat architecture decision.

Each question is tagged:

- **Audience** — who'd plausibly ask it (see readme table)
- **Anchor** — **named** (the question references a specific entity a resolver
  can look up — a Regulation, Role, Capability, Policy, Article, Control,
  etc.) or **open** (no specific entity named — "our," "we," "my service,"
  a free-text system description, or a request for an overview). This is
  the dimension that actually decides *how* a mechanism has to work, not
  just how hard the traversal is: a named question can be anchored and
  walked from there; an open one has nothing to anchor on and either needs
  a whole-graph deterministic aggregate (if the shape is structurally
  precise) or synthesis across many parts of the graph (if it isn't).
  Added retroactively across the whole catalog — including the original
  23 — when [`q-approach2.md`](./q-approach2.md) needed it to design a
  router, not just for the new questions below.
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

**S9–S15/M9–M14/H8–H15 added in a second pass** (2026-08-06), after building
the deterministic template router (`query_mechanism_v1.py`) exposed a real
risk: a catalog written to test the mechanism, by the people building the
mechanism, overfits. The original 23 questions all came from Legal Counsel,
Compliance Officer, Security Architect, Auditor, Risk Manager, and Policy
Manager framings — audiences who mostly ask questions *shaped like* what a
graph answers well (name an entity, walk a chain). Software Engineers,
Security Engineers, and Engineering Managers ask a different shape of
question just as often: no named entity at all ("is my service compliant,"
"where are we most exposed," "what should my team fix first"), because they
don't know or care what the graph calls things. Those are exactly the **open**
questions a template router and a purely entity-anchored LLM mechanism both
struggle with, and exactly what motivates the local/scoped/global router
design in `q-approach2.md`. Two of the new questions (H10, H15) turned out to
be genuine schema gaps rather than mechanism gaps — flagged, not routed around.

---

## Simple — single entity lookup, 1-hop traversal

Direct property reads or one edge-hop. No aggregation, no cross-regulation
reasoning. This tier is where a thin NL→Cypher translator should be enough.

| # | Question | Audience | Anchor | Graph pattern | Status | Grading |
|---|---|---|---|---|---|---|
| S1 | "What roles does GDPR define?" | Legal Counsel, Compliance Officer | named | `Regulation{id:GDPR-*} -DEFINES-> Role` | ✅ | set-match |
| S2 | "What's the text of CRA Article 13.1?" | Legal Counsel | named | `Regulation -EXPRESSES-> Requirement` property read | ✅ (corrected from "Article 11" — out of CRA's extraction scope, see `golden-answers.md`) | exact-match |
| S3 | "What obligations does the Manufacturer role carry under CRA?" | Compliance Officer | named | `Role{name:Manufacturer} -HAS-> Obligation` | ✅ | set-match |
| S4 | "What capabilities does 'Maintain Security Logging' require?" | Security Architect | named | `Obligation -REQUIRES-> Capability` | ✅ (corrected from "Maintain Security Monitoring" — no such obligation exists, see `golden-answers.md`) | set-match |
| S5 | "When does CRA become effective, and what's its current status?" | Compliance Officer | named | `Regulation` property read | ✅ | exact-match |
| S6 | "Which requirement does the 'Maintain Security Logging' obligation satisfy?" | Auditor | named | `Obligation <-SATISFIED_BY- Requirement` (inbound) | ✅ (corrected from "Maintain Structured Access Logging" — no such obligation exists, see `golden-answers.md`) | exact-match |
| S7 | "What policy governs the 'Security Logging' capability?" | Policy Manager | named | `Capability -GOVERNED_BY-> Policy` | ✅ real data (Helvex spike loaded the Policy layer over real capabilities) | exact-match |
| S8 | "List the standards under the Data Protection Policy." | Policy Manager | named | `Policy -SUPPORTED_BY-> Standard` | ✅ real data | set-match |
| S9 | "Which Controls exist under the Incident & Vulnerability Response Policy, and what are their statuses?" | Security Engineer | named | `Policy -SUPPORTED_BY-> Standard -IMPLEMENTED_BY-> Control` | ✅ real data | set-match |
| S10 | "What's the implementation status of the Encryption-at-Rest control?" | Software Engineer | named | `Control` property read (fuzzy title match) | ✅ real data | exact-match |

---

## Medium — multi-hop traversal, aggregation, cross-regulation joins

Requires walking 2+ edges, grouping/counting, or joining across regulations
through a shared node. This is where the domain model's actual value
proposition (convergence, traceability) starts to show up in answers — and
where a naive single-shot Cypher translation starts to strain.

| # | Question | Audience | Anchor | Graph pattern | Status | Grading |
|---|---|---|---|---|---|---|
| M1 | "Which capabilities are required by more than one obligation?" | Risk Manager | open | `Capability <-REQUIRES- Obligation`, `count() > 1` | ✅ | set-match |
| M2 | "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires." | Auditor, Security Architect | named | `Requirement -SATISFIED_BY-> Obligation -REQUIRES-> Capability`, chained | ✅ (corrected from "Art. 11" — out of CRA's extraction scope, see `golden-answers.md`) | exact-match |
| M3 | "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?" | Security Architect | named | Cross-regulation join *through* Capability | 🟡 re-verified: ran `find_capability_duplicates.py` against the live 68-capability graph — 0 candidate pairs at the default threshold, and manual review down to threshold 0.15 (57 pairs) found no genuine duplicates left to merge. That's by design (`nis2-extraction-methodology.md` / `gdpr-extraction-methodology.md`: real cross-regulation overlaps were converged onto shared capability ids at extraction time, not left for the merge workflow). `cap_security_logging_c4d9e2` is only sourced from CRA-1.0 and HELVEX-SOP-1.0 — NIS2 and GDPR were never extracted with a distinct logging capability to converge, so this is an extraction-scope gap, not an unmerged-duplicates gap | rubric — "all three regulations" can't be a true set-match until NIS2/GDPR extraction is revisited for a logging-shaped obligation, if one exists in the source text |
| M4 | "How many obligations does GDPR place on Data Processors vs. Data Controllers?" | Risk Manager, Legal Counsel | named | `Role -HAS-> Obligation`, grouped by Role, filtered to GDPR | ✅ | exact-match |
| M5 | "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?" | Legal Counsel | named | Cross-regulation Role comparison | 🧠 Role is *not* canonical by design (only Obligation/Capability converge) — needs semantic similarity over Role name/description, not a structural join | rubric |
| M6 | "Which obligations are backed by the weakest extraction confidence, and should be reviewed?" | Compliance Officer | open | `Obligation.confidence` threshold filter | ✅ | set-match against a fixed threshold (threshold choice itself is a rubric call) |
| M7 | "Show every path from a GDPR requirement down to a Control that verifies it." | Auditor | named | Full chain `Requirement→Obligation→Capability→Policy→Standard→Control` | 🟡 **status corrected** — this claimed ⛔ ("chain breaks after Capability") was stale; the Helvex spike wired Policy/Standard/Control over *real* CRA/NIS2/GDPR capabilities, not just Helvex-specific ones. 57 full GDPR chains exist today (confirmed live, see `golden-answers.md`), 31 current-evidence / 26 stale (16 through a `deprecated` Policy, 10 through a `draft`/`planned` branch) — prototyped in `query_mechanism_v1.py` | set-match (was exact-match "refusal") — the honesty test moves from "don't hallucinate a chain" to "don't present a stale/deprecated chain as current evidence" |
| M8 | "Which capabilities does our internal Helvex SOP regulation share with CRA?" | Security Architect | named | `Regulation{source_type:internal}` obligations converging on same `Capability` as `Regulation{source_type:external}` | ✅ real data confirmed live: `HELVEX-SOP-1.0`'s "maintain access logging for clinical trial systems" obligation and CRA's "maintain security logging" obligation both resolve to `cap_security_logging_c4d9e2` — no merge needed, convergence was authored directly (see `synthetic-data-spec.md`) | set-match |
| M9 | "How many Controls are currently overdue for review?" | Engineering Manager | open | `Control.next_review_date` in the past, excluding retired (`deprecated`) controls | ✅ real data | exact-match |
| M10 | "What percentage of our Policies are still draft or deprecated rather than approved?" | Engineering Manager | open | `Policy` grouped by `status`, ratio | ✅ real data | exact-match |
| M11 | "Which Capabilities have a governing Policy but zero implemented Controls underneath?" | Security Engineer | open | `Capability -GOVERNED_BY-> Policy -SUPPORTED_BY-> Standard -IMPLEMENTED_BY-> Control`, filtered to no `implemented` status in the set | ✅ real data — deeper than H2 (H2 stops at "has a Policy at all"; this checks whether the chain actually bottoms out in something implemented) | set-match |
| M12 | "Which Controls are overdue for review right now (not just 'due soon')?" | Security Engineer | open | `Control.next_review_date` in the past, excluding `deprecated` | ✅ real data — same underlying set as M9, different response shape (list vs. count); a good mechanism should answer both consistently from one query, not two independently-maintained ones | set-match |
| M13 | "Which Standards under the Data Protection & Security Policy are still in draft?" | Software Engineer | named | `Policy -SUPPORTED_BY-> Standard`, filtered to `draft` | ✅ real data — **golden answer is the empty set**, deliberately: all three Standards under this Policy are `implemented`/`reviewed`. Kept in the catalog specifically to test whether a mechanism can confidently say "none" instead of hallucinating a plausible-sounding draft Standard | set-match (∅) |
| M14 | "Which of our draft Policies are blocking GDPR readiness?" | Engineering Manager | named | `Policy{status:draft}` filtered to those governing GDPR-relevant Capabilities | 🧠 needs judgment about which draft Policy's governed Capabilities are actually GDPR-relevant, not just "is it draft" | rubric |

---

## Hard — gap analysis, situational reasoning, data we don't have yet

These are the questions that actually matter to the business ("are we
compliant?") but either need the unpopulated Policy/Standard/Control layer,
need to map free-text about a real system into the graph's vocabulary, or
need external state (audit evidence, dates, system architecture) the graph
was never meant to hold.

| # | Question | Audience | Anchor | What it needs | Status | Grading |
|---|---|---|---|---|---|---|
| H1 | "Are we compliant with GDPR Article 32?" | Risk Manager, Compliance Officer | named | Real obligation → real Policy → Standard → Control chain, plus Control pass/fail evidence | 🟡 **status corrected** — real per-sub-clause chains now exist (see `golden-answers.md`): 32.1a/b clean, 32.1c partial (one control still `planned`), 32.4 stale (governed only by a `deprecated` Policy) | rubric (compliant/partial/non-compliant + correct citation of the chain used to decide) — golden reasoning drafted |
| H2 | "Which capabilities required by CRA have no governing Policy yet?" (coverage gap analysis) | Risk Manager | named | `Capability` with no outbound `GOVERNED_BY`, scoped to CRA-derived capabilities | ✅ real data confirmed live: 55 of 68 capabilities ungoverned (13 governed) | set-match — golden value computed in `golden-answers.md` |
| H3 | "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?" | DevOps/Engineering | open | Map a free-text system description → Capability(s) it touches → check Controls | 🧠 needs NL-to-Capability mapping, but the Control layer it needs now exists (see H1) | rubric — golden reasoning drafted in `golden-answers.md`, grounded in H1's real chain |
| H4 | "Show me the audit evidence that our log retention control passed last quarter." | Auditor | named | `Control.evidence_ref` resolved against an external evidence store | ✅ real data confirmed live: `evidence://ci/log-retention-check/latest` | exact-match — correct answer is the `evidence_ref` pointer plus an explicit "the evidence store itself is out of scope," not a fabricated pass/fail |
| H5 | "NIS2 was updated — which of our Policies are now potentially out of date?" | Policy Manager | named | `Regulation -SUPERSEDED_BY-> Regulation` version diff, joined to real Policy layer | 🟡 **status corrected** — a real supersession now exists (`HELVEX-SOP-1.0 -SUPERSEDED_BY-> HELVEX-SOP-2.0`), plus a real `deprecated`/`draft` Policy pair to reason about staleness with; the NIS2-specific premise is still hypothetical | rubric — golden reasoning drafted |
| H6 | "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?" | Security Architect | named | Hypothetical capability insertion + redundancy detection across unmerged capabilities | 🧠 **premise correction** — an SBOM capability already exists (`cap_component_inventory_sbom_management_b5223c`, CRA-only); the real "hypothetical" is NIS2/GDPR convergence onto it, not minting a new node | rubric — golden reasoning drafted (today's correct answer: zero redundant coverage, no NIS2/GDPR obligation requires it yet) |
| H7 | "Which of our automated controls are due for review in the next 30 days?" | Auditor, Risk Manager | open | `Control.next_review_date` date-range filter | ✅ real data confirmed live: exactly 2 controls (`2026-08-15`, `2026-08-25`), anchored to the fixture's `2026-08-01` reference date | set-match — golden value computed in `golden-answers.md` |
| H8 | "I'm building a new microservice that stores customer PII in a database — what compliance capabilities should I be thinking about?" | Software Engineer | open | Map a free-text system description → the *set* of Capabilities it plausibly touches (broader than H3's single-endpoint, article-scoped version) | 🧠 needs NL-to-Capability mapping across several capabilities at once, not one | rubric — golden reasoning drafted in `golden-answers.md`, grounded in real capability names |
| H9 | "Our security scanner flagged missing rate-limiting on an endpoint that processes health data — does that block a GDPR-relevant control?" | Software Engineer | open | Map "rate-limiting" to a real Capability, then check its Control | ⛔ **the graph has no Capability for API rate-limiting/throttling at all** (confirmed live — no `Capability.name` matches "rate" or "throttl" except an unrelated GDPR transfer-mechanism capability) | rubric — the only correct answer is to say the graph doesn't model this and therefore can't determine a blocking verdict, not to invent a plausible-sounding one; a good answer may separately note the endpoint's *health-data* handling engages real Capabilities (Data Encryption, Access Control) worth checking instead |
| H10 | "Is my service, `checkout-api`, currently compliant?" | Software Engineer | open | A `Service`/`Application`/`System` node type linking deployed code to the Capabilities it implements | ⛔ **genuine schema gap, not a mechanism gap** — no such node type or edge exists anywhere in `ps-domain-concepts.md` or the loaded graph. This is arguably the single most natural question a Software Engineer would ask, and it's structurally unanswerable today regardless of how sophisticated the query mechanism is | rubric — the only correct answer is "the graph has no representation of your service to check against," not a fabricated status |
| H11 | "If an attacker exploited a missing MFA control today, which regulatory obligations across CRA/NIS2/GDPR would we be out of compliance with?" | Security Engineer | open | Map "MFA" → `cap_access_control_authentication_151816`, then walk **backward** from Capability to every Obligation that requires it, across regulations | 🧠 needs NL-to-Capability mapping plus a reverse multi-hop walk (Capability←Obligation←Role←Regulation), confirmed live: 7 real obligations across all three regulations converge here, including NIS2's explicit `obl_deploy_multi_factor_authentication_and_secured_communication_*` pair | rubric — golden reasoning drafted in `golden-answers.md`, grounded in the real 7-obligation set |
| H12 | "Across our whole Control set, where are we most exposed — what would an auditor flag first?" | Security Engineer | open | Synthesis across every gap/staleness signal in the graph at once (ungoverned capabilities, `planned` controls, overdue reviews, `deprecated`/`draft` policies) — no single entity to anchor on | 🧠 genuinely open-ended prioritization; this is the "global search" shape discussed in `q-approach2.md`, not a local lookup with extra steps | rubric — golden reasoning drafted, grounded in real counts (55 ungoverned capabilities, 1 `planned` control, 1 overdue control, 2 non-`approved` policies) |
| H13 | "Give me a one-paragraph summary of our overall compliance posture I can bring to the board." | Engineering Manager | open | Same whole-graph synthesis as H12, framed as an executive summary rather than a prioritized punch list | 🧠 the flagship "global" question — no named entity, requires an aggregated narrative a single Cypher query can't produce | rubric — a good answer must cite real numbers (68 capabilities, 13 governed/55 ungoverned, 4 policies: 2 approved/1 draft/1 deprecated) and explicitly flag the stale/deprecated/draft signals rather than smoothing them into a uniformly reassuring narrative |
| H14 | "What should my team prioritize this quarter to move the needle on compliance?" | Engineering Manager | open | Whole-graph synthesis plus a recommendation/prioritization judgment | 🧠 open-ended; needs to turn gap signals into a concrete, actionable short list | rubric — a good answer names specific real items (finish the `planned` Vulnerability Patch SLA Check, review the overdue Incident Triage SLA control, resolve the `draft` Clinical Data Integrity Policy, decide the fate of the `deprecated` Legacy Asset & Personnel Security Policy), not generic advice |
| H15 | "How long, on average, does it take a Standard to go from draft to implemented in our organization?" | Engineering Manager | open | A state-transition history per Standard (when it moved `draft`→`implemented`) | ⛔ **genuine schema gap** — `Standard` (and `Control`, `Policy`) carry only a current `implementation_status`, no timestamped history of prior status changes. Unanswerable regardless of mechanism sophistication, same category as H10 | rubric — the only correct answer is "this isn't tracked; here's what would need to change to answer it," not a fabricated average |

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
- **The second pass (S9–S15/M9–M14/H8–H15) exists to fight overfitting to
  audiences who ask graph-shaped questions.** Software Engineers, Security
  Engineers, and Engineering Managers produced a noticeably different mix:
  more **open**-anchor questions (no named entity — "my service," "our
  posture," "what should we prioritize") and two genuine **schema** gaps
  (H10: no Service/Application node exists to check "is *my* service
  compliant" against; H15: no state-transition history exists to compute
  time-in-status metrics). Neither gap is a query-mechanism problem — no
  amount of LLM reasoning fixes data that was never modeled — and both are
  flagged rather than routed around, the same discipline already applied to
  M3 and H6's premise correction. The **open**-anchor questions (M9–M12,
  H8, H9, H11–H14) are what actually motivate `q-approach2.md`'s router: an
  entity resolver finds nothing to anchor on for roughly a third of this
  catalog now, which the original 23-question set never surfaced.
