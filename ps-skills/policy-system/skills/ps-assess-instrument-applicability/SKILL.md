---
name: ps-assess-instrument-applicability
description: Interview a Compliance Officer about their company's markets/geographies and products/services, confirm the captured input, then produce a tiered (likely/possible/excluded) candidate list of EU regulatory instruments from general regulatory knowledge, each with its CELEX id when known.
---

# ps-assess-instrument-applicability

## Purpose

Help a Compliance Officer seeding a company into the Policy System compliance
graph figure out which EU regulatory instruments plausibly apply to that
company, based on the markets/geographies it serves and the products/services
it offers. The EU has not published official product/market-to-instrument
guidance, so this skill uses the model's own general knowledge of EU
regulatory instruments to produce a tiered candidate list instead of leaving
the Compliance Officer to either skip the exercise or do it by hand.

**Scope:** interviews the Compliance Officer for the two required inputs,
confirms them before proceeding, produces a tiered candidate list from
general model knowledge only, cross-checks each candidate's determinable
CELEX against the compliance graph's ingestion status via
`check_instrument_ingestion_status`, and brackets the delivered candidate
list with a prominent non-authoritative/incomplete/not-a-substitute
disclaimer. Any internal or model-call failure during assessment
generation that isn't already covered by the connector-unreachable
handling in Process step 5 is reported to the Compliance Officer as a
fixed, generic, actionable message — never a stack trace, exception class
name, or internal identifier.

**Deliverable:** the captured markets/geographies and products/services
(echoed back for confirmation), and, once confirmed, a structured candidate
list of EU regulatory instruments, each tiered `likely` / `possible` /
`excluded`, with the specific signal or exclusion reason behind its tier,
its CELEX identifier when known (or an explicit statement that the CELEX
could not be determined), and its ingestion-status flag (`ingested` /
`not_yet_ingested` / `unknown` / "not checked — no CELEX").

## On Load

Two connector names are recognised, and exactly one of them is expected to
be present in any given session:

- `policy-system-graph` — the connector this plugin declares, pointing at a
  hosted PS Service.
- `policy-system-graph-local` — a user-registered local MCP server bridging
  to a PS Service running on the same machine (local test; see the user
  guide's step 8).

Use whichever is present; if both are, prefer `policy-system-graph` and
fall back to `policy-system-graph-local` only if the former is unreachable.
Any other name is not a PS Service connector. A connector that is present
under the right name but does not expose a `check_instrument_ingestion_status`
tool is **not** a PS Service connector either, whatever it is named — report
it as unreachable rather than proceeding against it. From here on, "the PS
Service connector" means the one selected here.

`check_instrument_ingestion_status` is the tool this skill uses (Process
step 5) to cross-check every candidate's determinable CELEX against the
compliance graph's ingestion status, once tiering (step 4) has produced the
candidate list. The interview, confirmation, and tiering steps run entirely
on the model's own reasoning and need no tool call; only step 5 calls out to
PS Service, and it does so exactly once per assessment, batching every
candidate's determinable CELEX into a single call rather than one call per
candidate.

## Core Principles

- Never generate an assessment before the Compliance Officer has explicitly
  confirmed the captured markets/geographies and products/services back —
  see Process step 3.
- Never proceed past the interview with markets/geographies or
  products/services missing or empty — re-prompt for the missing piece
  instead of tiering against incomplete input (Process step 2).
- Tier every candidate from the model's own general EU-regulatory knowledge
  only — never the compliance graph, and never a curated catalog. Neither is
  consulted to produce candidates; the graph is only ever used (Process
  step 5) to flag whether an already-produced candidate's CELEX is already
  ingested, never to decide which candidates exist in the first place.
- Never fabricate a CELEX identifier. When a candidate's CELEX cannot be
  determined, the entry states this explicitly instead.
- Never silently omit an excluded candidate, or leave a likely/possible
  candidate's tier unexplained — every entry states the signal or reason
  behind its tier.
- Never coerce a "CELEX not determinable" candidate's ingestion-status flag
  into `unknown` — "CELEX not determinable" (step 4, AC-BI-007) and an
  ingestion-status flag of `unknown` (step 5, AC-BI-008) answer different
  questions and must stay visually distinct; a candidate with no
  determinable CELEX is always labelled "not checked — no CELEX" instead.
- Never abort the whole assessment because the ingestion-status cross-check
  could not be completed — if the PS Service connector is unreachable when
  step 5 calls `check_instrument_ingestion_status`, report every candidate's
  ingestion-status flag as `unknown` and still produce the rest of the
  artifact in full.
- If any other internal or model-call failure occurs during assessment
  generation — anywhere in steps 1–6, and distinct from the
  connector-unreachable case step 5 already handles — never show the
  Compliance Officer the underlying exception, stack trace, or any internal
  identifier. Show exactly this fixed message instead: "I hit an
  unexpected internal error while generating this assessment. Please try
  again; if the problem continues, contact your PS Service administrator."

## Process

1. **Interview the Compliance Officer.** Ask for, at minimum, both of the
   following:
   - the markets/geographies the company serves;
   - the products/services the company offers.

   Ask open questions and follow up as needed to get concrete answers, but
   do not require more than these two at this stage.

2. **Check for missing input.** If, after the interview, either
   markets/geographies or products/services is missing or empty, re-prompt
   specifically for the missing piece — do not proceed to step 3 or generate
   any assessment from incomplete input. Repeat step 2 until both are
   non-empty.

3. **Echo and confirm.** Once both markets/geographies and products/services
   are captured, echo them back to the Compliance Officer verbatim and wait
   for their explicit confirmation before generating anything. If the
   Compliance Officer requests a correction, capture it and echo the
   corrected values back for confirmation again — do not proceed on an
   implicit or partial confirmation.

4. **Generate the tiered candidate list.** Once confirmed, produce a
   structured list of candidate EU regulatory instruments using the model's
   own general knowledge of EU regulatory instruments — explicitly **not**
   the compliance graph and **not** a curated catalog; neither is queried at
   this stage. Tier every candidate as one of:

   - **likely** — state the specific market/product signal (from the
     confirmed input) that drove this tier.
   - **possible** — state the specific market/product signal that drove
     this tier, and what would need to be confirmed to firm it up.
   - **excluded** — state the reason for exclusion; an excluded candidate is
     never silently omitted from the list.

   For every candidate, include its CELEX identifier when known. When it
   cannot be determined, state this explicitly (e.g. "CELEX not
   determinable") rather than fabricating one.

   This step produces every candidate field except its ingestion-status
   flag, which step 5 adds next — do not emit output to the Compliance
   Officer yet; the shape below is completed and delivered only after
   step 5.

5. **Cross-check ingestion status.** Once step 4's tiered candidate list
   exists, collect the CELEX identifier of every candidate for which one was
   determined in step 4 (skip only the candidates explicitly marked "CELEX
   not determinable") and make **one batched call** to
   `check_instrument_ingestion_status` on the PS Service connector selected
   at On Load, passing every collected CELEX id in a single `celex_ids`
   list — never one call per candidate. Merge the tool's returned `statuses`
   value back into the corresponding candidates as an **ingestion-status**
   flag:

   - `ingested` — the tool reported this CELEX as already present in the
     compliance graph.
   - `not_yet_ingested` — the tool reported this CELEX as not yet present in
     the compliance graph.
   - `unknown` — the tool itself could not determine ingestion status (the
     connector was reachable and the tool call completed, but a graph-side
     problem meant it could not tell); the tool always still returns a
     `statuses` entry for every CELEX it was asked about in this case, never
     a call-level error.

   A candidate with no determinable CELEX was never included in the batch
   call at all, and its ingestion-status flag is a fourth, distinct literal
   label instead of any of the above:

   - **"not checked — no CELEX"** — no CELEX was available to check
     ingestion status against. Never coerce this into `unknown`: "CELEX not
     determinable" (step 4, AC-BI-007) answers whether the model could
     identify the instrument's CELEX at all; the ingestion-status flag
     (this step, AC-BI-008) answers the separate question of whether an
     already-identified instrument is already ingested into the compliance
     graph. Collapsing the two into one `unknown` label would erase that
     distinction for the reader.

   If the PS Service connector itself is unreachable when this call is
   attempted — a transport/connector-level failure, distinct from a
   reachable connector whose tool call reports a graph-side problem (folded
   to `unknown` per candidate above, inside the tool's own successful
   response) — do not abort the assessment. Instead:

   - report every candidate that had a determinable CELEX with an
     ingestion-status flag of `unknown`;
   - report every candidate with no determinable CELEX still as
     "not checked — no CELEX" (unchanged — it was never sent to the tool
     regardless of connector state);
   - produce the rest of the artifact — every field step 4 already
     produced — in full, exactly as generated. Never fail the whole
     assessment because the cross-check could not be completed.

   | Tool call outcome                                                                                                                                             | Ingestion-status flag reported per candidate                                                                                                              |
   | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Candidate had a determinable CELEX; tool reported it present in the graph                                                                                     | `ingested`                                                                                                                                                |
   | Candidate had a determinable CELEX; tool reported it absent from the graph                                                                                    | `not_yet_ingested`                                                                                                                                        |
   | Candidate had a determinable CELEX; tool call succeeded but could not resolve this CELEX (graph-side problem, folded internally by the tool)                  | `unknown`                                                                                                                                                 |
   | Candidate had no determinable CELEX (step 4's "CELEX not determinable")                                                                                       | "not checked — no CELEX" — never `unknown`, regardless of connector state                                                                                 |
   | PS Service connector itself unreachable when attempting the call (transport/connector failure, distinct from a reachable connector reporting a graph problem) | `unknown` for every candidate that had a determinable CELEX; "not checked — no CELEX" unchanged for the rest; rest of the artifact still produced in full |

6. **Output**, in this shape, only once step 5 has completed (either with
   real statuses or with the degraded all-`unknown` fallback above). The
   disclaimer brackets the candidate list — rendered as its own top-level
   block, at the same prominence as the `Candidate instruments:` block
   itself, immediately before it and restated immediately after it. Never
   demote the disclaimer to a trailing footnote or a sub-bullet under any
   individual candidate (AC-BI-011):

   ```text
   Captured input (confirmed):
     Markets/geographies: <value>
     Products/services: <value>

   Disclaimer: This assessment is non-authoritative, may be incomplete, and
   is not a substitute for legal or compliance review.

   Candidate instruments:
     [likely] <instrument name> (CELEX <id or "not determinable">)
       Signal: <specific market/product signal>
       Status: <ingestion-status flag>
     [possible] <instrument name> (CELEX <id or "not determinable">)
       Signal: <specific market/product signal, plus what would confirm it>
       Status: <ingestion-status flag>
     [excluded] <instrument name> (CELEX <id or "not determinable">)
       Reason: <specific exclusion reason>
       Status: <ingestion-status flag>
     ...

   Disclaimer (restated): non-authoritative, may be incomplete, not a
   substitute for legal or compliance review.
   ```

## Example interaction

> **Compliance Officer:** I need to run an applicability assessment for our
> company.
>
> **Skill:** To get started I need two things: the markets/geographies your
> company serves, and the products/services it offers. What
> markets/geographies does your company serve?
>
> **Compliance Officer:** We operate across the EU — customers in most
> member states.
>
> **Skill:** Got it. And what products/services does your company offer?
>
> **Compliance Officer:** Not sure that's relevant, can we skip it for now?
>
> **Skill:** I can't generate an assessment without it — products/services
> offered is required along with markets/geographies. What products/services
> does your company offer?
>
> **Compliance Officer:** Fair enough. We run a SaaS platform that processes
> online card payments for merchants based in the EU.
>
> **Skill:** Here's what I've captured — please confirm before I generate
> the assessment:
>
> ```text
> Markets/geographies: EU (customers across most member states)
> Products/services: SaaS platform processing online card payments for
> EU-based merchants
> ```
>
> Is this correct?
>
> **Compliance Officer:** Yes, that's correct.
>
> **Skill:** _(tiers the four candidates from step 4 first, then, per step 5,
> collects every candidate's determinable CELEX — PSD2, GDPR, and MiCA;
> DORA's is not determinable so it is excluded from the batch — and makes
> one batched call before presenting anything)_
>
> ```text
> check_instrument_ingestion_status(celex_ids=["32015L2366", "32016R0679", "32023R1114"])
> → {"statuses": {"32015L2366": "ingested", "32016R0679": "not_yet_ingested", "32023R1114": "unknown"}}
> ```
>
> ```text
> Captured input (confirmed):
>   Markets/geographies: EU (customers across most member states)
>   Products/services: SaaS platform processing online card payments for
>   EU-based merchants
>
> Disclaimer: This assessment is non-authoritative, may be incomplete, and
> is not a substitute for legal or compliance review.
>
> Candidate instruments:
>   [likely] Revised Payment Services Directive (PSD2) (CELEX 32015L2366)
>     Signal: processes online card payments for EU-based merchants —
>     squarely a "payment service provider" activity within PSD2's scope.
>     Status: ingested
>   [likely] General Data Protection Regulation (GDPR) (CELEX 32016R0679)
>     Signal: processing cardholder and merchant personal data as part of
>     the SaaS platform triggers general EU data-protection obligations.
>     Status: not_yet_ingested
>   [possible] Digital Operational Resilience Act (DORA) (CELEX not
>   determinable)
>     Signal: if the platform's merchant customers include EU-regulated
>     financial entities, DORA's ICT third-party risk-management provisions
>     could reach it as a critical ICT service provider; this needs
>     confirmation of whether the merchant base includes regulated financial
>     entities. The CELEX for the currently-applicable version could not be
>     confidently determined, so it is stated as not determinable rather
>     than guessed.
>     Status: not checked — no CELEX
>   [excluded] Markets in Crypto-Assets Regulation (MiCA) (CELEX 32023R1114)
>     Reason: the company processes card payments for merchants, not
>     crypto-asset issuance, custody, or exchange services — MiCA's scope
>     does not reach it.
>     Status: unknown
>
> Disclaimer (restated): non-authoritative, may be incomplete, not a
> substitute for legal or compliance review.
> ```
>
> This one worked example demonstrates all four ingestion-status flags:
> `ingested` (PSD2), `not_yet_ingested` (GDPR), `unknown` (MiCA — the tool
> call succeeded but could not resolve this CELEX), and
> "not checked — no CELEX" (DORA — no CELEX was ever submitted).

## Example interaction — degraded path (PS Service connector unreachable)

A shorter continuation from the same confirmed input, showing what happens
when the PS Service connector itself cannot be reached at step 5, rather
than a reachable connector reporting a graph-side problem.

> **Compliance Officer:** Yes, that's correct.
>
> **Skill:** _(tiers the same four candidates, then attempts the batched
> `check_instrument_ingestion_status` call for PSD2, GDPR, and MiCA's
> determinable CELEX ids)_
>
> ```text
> check_instrument_ingestion_status(celex_ids=["32015L2366", "32016R0679", "32023R1114"])
> → connector "policy-system-graph" unreachable: connection refused
> ```
>
> **Skill:** I couldn't reach PS Service to check ingestion status for this
> assessment's candidates, so every candidate's status below is reported as
> `unknown` rather than skipping the check silently. Here's the full
> assessment:
>
> ```text
> Captured input (confirmed):
>   Markets/geographies: EU (customers across most member states)
>   Products/services: SaaS platform processing online card payments for
>   EU-based merchants
>
> Disclaimer: This assessment is non-authoritative, may be incomplete, and
> is not a substitute for legal or compliance review.
>
> Candidate instruments:
>   [likely] Revised Payment Services Directive (PSD2) (CELEX 32015L2366)
>     Signal: processes online card payments for EU-based merchants —
>     squarely a "payment service provider" activity within PSD2's scope.
>     Status: unknown
>   [likely] General Data Protection Regulation (GDPR) (CELEX 32016R0679)
>     Signal: processing cardholder and merchant personal data as part of
>     the SaaS platform triggers general EU data-protection obligations.
>     Status: unknown
>   [possible] Digital Operational Resilience Act (DORA) (CELEX not
>   determinable)
>     Signal: if the platform's merchant customers include EU-regulated
>     financial entities, DORA's ICT third-party risk-management provisions
>     could reach it as a critical ICT service provider; this needs
>     confirmation of whether the merchant base includes regulated financial
>     entities. The CELEX for the currently-applicable version could not be
>     confidently determined, so it is stated as not determinable rather
>     than guessed.
>     Status: not checked — no CELEX
>   [excluded] Markets in Crypto-Assets Regulation (MiCA) (CELEX 32023R1114)
>     Reason: the company processes card payments for merchants, not
>     crypto-asset issuance, custody, or exchange services — MiCA's scope
>     does not reach it.
>     Status: unknown
>
> Disclaimer (restated): non-authoritative, may be incomplete, not a
> substitute for legal or compliance review.
> ```
>
> Every candidate that had a determinable CELEX (PSD2, GDPR, MiCA) is shown
> as `unknown`; DORA's "not checked — no CELEX" is unchanged from the
> happy-path example, since it was never going to be sent to the tool
> regardless of connector state. The assessment is still produced in full —
> the connector failure never aborts it.

## Guardrails

- Never generate any part of the candidate list before the Compliance
  Officer has explicitly confirmed the captured markets/geographies and
  products/services.
- Never proceed to tiering with markets/geographies or products/services
  missing or empty — always re-prompt instead.
- Never tier a candidate from the compliance graph or a curated catalog —
  tiering uses the model's own general EU-regulatory knowledge only.
- Never fabricate a CELEX identifier for any candidate — state "CELEX not
  determinable" when it is not known.
- Never omit an excluded candidate, or leave a likely/possible candidate's
  tier unexplained.
- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never a
  direct graph connection, a repo-local script, or a spawned external
  binary. `check_instrument_ingestion_status` (Process step 5) is called
  through this same recognised connector, like every other tool call this
  skill makes — never a separate or ad hoc channel.
- `check_instrument_ingestion_status` requires no elevated `AccessRole`: it
  is a plain graph read, exactly like the read-only tools other PS Service
  skills call. This is a deliberate distinction, not an oversight — contrast
  it with `ps-manage-access-roles` and `ps-list-audit-events`, whose tools
  _do_ require the caller to hold `SystemAdmin` or above before they will
  even attempt the call. This skill's Compliance Officer needs no such role
  to run an applicability assessment.
- Never coerce a "CELEX not determinable" candidate's ingestion-status flag
  into `unknown`, and never coerce a connector-unreachable degraded result
  into anything other than `unknown` for every candidate that had a
  determinable CELEX — the two failure/absence states must stay visually
  distinct, per Process step 5.
- Never abort the assessment because `check_instrument_ingestion_status`
  could not be reached or could not resolve a candidate — always produce
  the rest of the artifact in full, per Process step 5's degraded path.
- The delivered artifact always brackets its candidate list with the
  disclaimer (AC-BI-011): the same non-authoritative/may-be-incomplete/
  not-a-substitute-for-legal-or-compliance-review wording appears
  immediately before the `Candidate instruments:` block and is restated
  immediately after it, at the same prominence as the list itself — never
  demoted to a trailing footnote or a sub-bullet under any individual
  candidate.
- Any internal or model-call failure during assessment generation that
  isn't already covered by Process step 5's connector-unreachable handling
  surfaces to the Compliance Officer only as the fixed message defined in
  Core Principles (AC-BI-012) — never the underlying exception, a stack
  trace, or any internal identifier.
