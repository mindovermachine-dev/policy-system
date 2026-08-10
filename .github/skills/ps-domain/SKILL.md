---
name: ps-domain
description: >-
  Grounds the agent in the Policy System domain model — graph schema, ID
  conventions, query shapes, and provenance discipline — for answering
  compliance questions against the policy_system graph.
metadata:
  copyright: "© 2026 Cartman ApS. All rights reserved."
---

# Policy System Domain

You answer questions about the Policy System compliance graph using the tools
available in your harness. You have no other source of truth about this graph's
contents — do not answer from memory or plausible-sounding inference.

## Graph Schema

Re-read this before every query you write, not just once.

**Relationship chains** — direction matters. An edge only matches the direction
shown; querying it backwards silently returns zero rows, it does not error:

```
(:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)-[:REQUIRES]->(:Capability)
  -[:GOVERNED_BY]->(:Policy)-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(:Control)
(:Regulation)-[:EXPRESSES]->(:Requirement)-[:SATISFIED_BY]->(:Obligation)
(:Regulation)-[:SUPERSEDED_BY]->(:Regulation)
```

**Exact property names per label** — a query using any other property name for
a label will silently match nothing, not error:

- `Regulation`: `id` (e.g. `'GDPR-1.0'`, `'CRA-1.0'`), `title`, `source_type`,
  `jurisdiction`, `effective_date`, `version`, `status`. **No `name` property.**
- `Role`: `id`, `name`, `description`. **No `title`.**
- `Requirement`: `id` (e.g. `'GDPR-1.0_req_art_32.1a'`), `text`, `type`, `status`.
  **No `title`.**
- `Obligation`: `id`, `text`, `confidence`, `obligation_type`. **No `title`,
  no `source_ref`** (provenance is transitive — see Provenance below).
- `Capability`: `id`, `name`, `description`, `type`, `status`.
- `Policy`: `id`, `title`, `description`, `owner_id`, `status`, `version`.
  **No `name`.**
- `Standard`: `id`, `title`, `description`, `implementation_status`, `version`.
- `Control`: `id`, `type`, `title`, `description`, `implementation_status`,
  `execution_frequency`, `last_test_date`, `next_review_date`, `evidence_ref`.

**Edge properties**: `source_ref` (string) lives on `DEFINES` and `EXPRESSES`
edges only — the article/section where the Regulation defines that Role or
expresses that Requirement (e.g. `"Art. 13(1)"`). All other edges carry no
properties.

**Requirement IDs encode article + sub-clause**, e.g. `'GDPR-1.0_req_art_18.2b'`.
A single article commonly has several Requirement IDs (a base clause plus
lettered sub-clauses: `..._art_18.2`, `..._art_18.2a`, `..._art_18.2b`). The
sub-clause suffix extends past the bare article number. To find every
Requirement under one article, use `req.id CONTAINS "art_18"` or
`req.id STARTS WITH "GDPR-1.0_req_art_18"`. Do NOT use
`req.id ENDS WITH "art_18"` to mean "this article and everything under it" —
that only matches an ID ending *exactly* there and silently misses every
lettered sub-clause.

**`STARTS WITH` over-matches across article-number boundaries** (FINDING-002):
`req.id STARTS WITH "CRA-1.0_req_art_13.1"` also matches `art_13.11` through
`art_13.19` — the prefix is a textual match, not a numeric one. To scope to
article 13.1 and its lettered sub-clauses only, match the prefix and then
verify the character immediately after it is a letter or end-of-string (e.g.
filter results client-side, or anchor with a second condition that excludes
`.1` followed by another digit).

## ID Conventions

| Label | Shape | Example |
|---|---|---|
| Regulation | `{SHORT}-{VERSION}` | `CRA-1.0` |
| Role | `role_{slug}_{hash}` | `role_manufacturer_a1b2c3` |
| Requirement | `{REG}_req_art_{ARTICLE}` | `CRA-1.0_req_art_13.1` |
| Obligation | `obl_{slug}_{hash}` | `obl_conduct_and_document_cybersecurity_risk_assessment_5f0fa8` |
| Capability | `cap_{slug}_{hash}` | `cap_vulnerability_management_55d0c4` |
| Policy | `pol_{slug}_{hash}` | `pol_data_protection_security_policy_8e4c18` |
| Standard | `std_{POLICY}_{VERSION}` | `std_pol_data_protection_security_policy_8e4c18_v1` |
| Control | `ctrl_{STANDARD}_{TYPE}` | `ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated` |

Obligation, Capability, and Policy IDs are deliberately **regulation-independent**:
the same node is reused across regulations. Never assume an ID embeds the
regulation, role, or article it relates to for these labels — discover the
connections by traversing edges, not by parsing IDs.

## Two-Layer Content Model

The graph holds two layers, joined at Capability:

- **Regulatory layer** (read-only, ingested from official sources):
  Regulation → Role / Requirement → Obligation. This is *what the law demands*.
- **Organizational layer** (authored by the organization, lifecycle-managed):
  Capability → Policy → Standard → Control. This is *how we meet it*.

`Capability` is the hinge: Obligations (regulatory "what") `REQUIRE`
Capabilities, and Capabilities are `GOVERNED_BY` Policies (organizational
"how"). A question about "our obligations" walks the regulatory layer; a
question about "our coverage" or "our posture" crosses the hinge into the
organizational layer.

Status fields are layer-specific and governance-significant:
- Policy: `draft` / `approved` / `deprecated`
- Standard: `draft` / `implemented` / `reviewed` / `deprecated`
- Control: `planned` / `implemented` / `reviewed` / `deprecated`

A chain through a `deprecated` Policy or a `planned` Control is **not current
evidence**.

## Canonical Definitions

These boundary rules are not derivable from the schema alone — pin them
explicitly, and apply them consistently across every answer:

- **Overdue**: a Control's `next_review_date` has passed. Deprecated Controls
  are excluded — a deprecated Control has left the review cycle, it has not
  failed it.
- **Stale**: the chain from Capability to a current Control is *broken* (no
  `IMPLEMENTED_BY` Control in `implemented` or `reviewed` status) — not merely
  an overdue review on an otherwise-live chain. A live Control with a lapsed
  review is "overdue," not "stale." Do not conflate the two when counting.

## CLI Command Surface

Where a `ps` CLI is available in your harness, reach the graph through it, not
through raw Cypher. Prefer a deterministic command over `ps cypher` whenever
one fits the question — freelancing Cypher for something a command already
answers deterministically is a wrong approach even if the answer comes out
right.

| Command | Use for | Query shape below |
|---|---|---|
| `ps query template "<question, verbatim>"` | Structural questions matched by fixed shape: roles, obligations, requirement text, policy/standard/control lookups by name, aggregate counts | Shape 1, templated cases of Shape 5 |
| `ps query catalog <capability-id-or-name>` | Full chain through one named Capability: Regulation → Role → Obligation → Capability → Policy → Standard → Control, plus the Requirement text | Shape 4 |
| `ps capabilities list [--filter TEXT] [--ungoverned]` | Discover the exact Capability id/name before calling `query catalog`, or list ungoverned Capabilities | Shape 2, Capability only |
| `ps templates` | List every pattern `query template` recognizes — check command coverage before assuming a gap means the data doesn't exist | coverage check, not a shape |
| `ps cypher "<MATCH/RETURN query>"` | Escape hatch — read-only, write clauses rejected before execution. Use ONLY when nothing above fits: anchored walks not rooted at a Capability, entity-vocabulary discovery for labels other than Capability, or aggregates the template router doesn't recognize | Shape 2 (non-Capability), Shape 3, untemplated Shape 5 |

Command-selection order for any question:

1. Does it match a pattern in `ps templates`? → `ps query template`.
2. Is it anchored on a named Capability, asking about its regulatory-to-organizational chain or coverage? → resolve the id with `ps capabilities list --filter <text>` if you don't already have it, then `ps query catalog <id>`.
3. Otherwise, and only otherwise → `ps cypher`, applying every rule in this skill exactly as if you were querying directly — the escape hatch does not relax the schema, ID, or provenance discipline below.

`NO_TEMPLATE_MATCH` from `ps query template` means "fall through to the next
step in this order," not "the data doesn't exist" — it is a routing signal,
not a refusal to relay to the user.

Global flags (`--host`, `--port`, `--graph`, `--format text|json`) are accepted
both before and after the subcommand. Use `--format json` when you need to
parse fields out of the result rather than read a table.

## Canonical Query Shapes

These describe the underlying retrieval strategy regardless of access
mechanism; where a `ps` CLI command implements one (see above), use it instead
of writing the Cypher yourself.

Route by what the question asks for, not by surface keywords:

1. **Regulation text / article lookup** ("what does article X say", fines,
   deadlines): anchor on `Regulation.id` + Requirement ID pattern. Match all
   sub-clauses with `STARTS WITH '{REG}_req_art_{N}'`, return `text` and the
   `EXPRESSES` edge's `source_ref`.
2. **Entity-vocabulary discovery** ("is there anything about X", or when a
   name from the question doesn't obviously map): before concluding absence,
   list the real vocabulary — e.g. `MATCH (c:Capability) RETURN c.name` — and
   pick the closest real match by semantic judgment, or report honestly that
   none exists.
3. **Anchored walk** ("what applies to the manufacturer", "what covers
   encryption"): resolve the entity via shape 2, then traverse edges in schema
   direction only — e.g. Role `HAS` Obligation `REQUIRES` Capability
   `GOVERNED_BY` Policy.
4. **Cross-layer / coverage questions** ("are we covered for", "what governs"):
   start regulatory (Obligation), cross `REQUIRES` to Capability, then
   `GOVERNED_BY` → `SUPPORTED_BY` → `IMPLEMENTED_BY`. Report the status of
   every Policy/Standard/Control on the path.
5. **Whole-graph aggregates** ("how many", "where are we most exposed",
   "overall posture"): aggregation over unverified rows is a known silent-error
   class. Write aggregate queries that compute counts directly in Cypher
   (`RETURN count(...)`), not queries that return rows for you to count
   yourself. Cross-check surprising numbers with a second, differently-shaped
   query before stating them.
6. **Refusal shape**: if discovery (shape 2) finds no plausible match and a
   verified query returns zero rows, the answer is "no such X is tracked in
   the graph" — stated plainly, with what you checked.

## Rules — Non-Negotiable

1. **Never state a fact about the graph's contents** (an ID, a status, a
   count, a chain) that you did not just retrieve via a tool call in this
   conversation.
2. **Zero rows is not proof of absence.** A query you wrote may use the wrong
   property name or direction. Before concluding something isn't in the graph,
   re-check property names against the schema above, or list the label's real
   vocabulary, then retry. Concluding "not tracked" from an unverified query
   is treated as a wrong answer, same as a fabricated one.
3. **Account for every row returned.** When a query returns multiple rows,
   your answer must cover all of them, not a subset you find most relevant.
   Dropping rows you actually retrieved is treated the same as fabricating.
4. **State governance status on organizational chains.** Whenever your answer
   relies on a chain passing through Policy, Standard, or Control, state the
   status of each. Presenting a deprecated/planned chain without that caveat
   is treated as a wrong answer even if the chain is real.
5. **Cite real IDs, not descriptions.** "Several obligations require this" is
   not an acceptable citation; the specific obligation IDs are.
6. **Provenance chain discipline.** Every regulatory fact must carry its
   source chain: Regulation → `EXPRESSES`/`DEFINES` edge `source_ref` (the
   article/section) → Requirement/Role → onward. Obligation and Capability
   have no `source_ref` property by design — their provenance is transitive,
   established by walking inbound `SATISFIED_BY` / `REQUIRES` edges back to
   the Requirements and Regulation articles. Cite that walked chain.
7. **Narrowing discipline.** When asked what depends on, or breaks with, a
   named node, cite only chains that *route through* that node — not sibling
   chains that reach the same downstream target by a different path. Scope
   blast-radius claims to the actual traversed chain, not the union of
   everything nearby that happens to be reachable.
8. **Unit-of-counting discipline.** Before answering "how many," state what
   you are counting — chains, distinct Controls, distinct Obligations, etc.
   These yield different numbers over the same graph. A numerically correct
   count at the wrong granularity is a wrong answer.
9. **No freelancing past a deterministic command.** Where the CLI Command
   Surface above lists a command for the question's shape, use it. Reaching
   for `ps cypher` when `ps query template` or `ps query catalog` already
   covers the question is treated as a wrong approach, independent of whether
   the resulting answer happens to be correct.

## Pre-Submit Verification (mandatory)

Before you send your final answer, run this four-part check explicitly in
your own output — not a tool call, a required reasoning step. Retrieving
correct data and then dropping, miscounting, or mis-caveating it on the way
to the final sentence is treated as a wrong answer, same as fabrication.

1. **Requirements restatement.** Before drafting, state plainly what the
   question demands: an exact count, an exhaustive list, a specific ID, a
   comparison, or a refusal-check. Write this down first — the checks below
   have nothing to verify against otherwise.
2. **Recount check.** For every number in your draft answer, recompute it
   against the retrieved rows — use the `row_count` field in `--format json`
   output as the canonical count, not your own tally of a printed table or
   your running memory of how many rows scrolled by. If your draft states a
   number, it must equal a `row_count` (or a `count(...)` you ran in `ps
   cypher`) you actually retrieved this conversation, not an estimate.
3. **Completeness check.** For every entity or chain your answer cites,
   confirm against rules 4/5/6 above: governance status stated for every
   organizational-layer node, real IDs cited (not descriptions), and the full
   provenance chain traced back to source.
4. **Known-gaps lookup.** Before answering *or* refusing a question about
   fines, penalties, or enforcement figures, check the Known-Gaps Registry
   below first. A match there means refuse immediately, citing the registry
   entry — do not run further exploratory queries first, and do not reach for
   any tool outside `ps` (including web search) to fill the gap. This graph
   and this CLI are the only source of truth available to you; there is no
   sanctioned fallback.

## Known-Gaps Registry

Confirmed absent from the graph — refuse immediately per Pre-Submit
Verification step 4, do not search further and do not use any tool outside
`ps` to compensate. Entries here are added only after a schema-verified query
returns zero rows for a real property/label combination; never added on
suspicion.

| Topic | What's missing | Confirmed by |
|---|---|---|
| GDPR administrative fines | Art. 83 fine tiers/figures (the €20m / 4%-of-turnover cap and its conditions) are not ingested as Requirement/Obligation text. | dev-set LC-E2 |
| NIS2 "significant incident" threshold | Art. 23(3) — the enforcement/reporting-threshold text defining when an incident is "significant" — is not ingested. | dev-set RM-E2 |
| NIS2 penalties | Art. 34 fine tiers/figures are not ingested. | dev-set EM-M3 |
| CRA penalties | Art. 64 fine tiers/figures are not ingested. | dev-set EM-M3, SEC-H3 |

This registry tracks the same gap as `spikes/skill-transfer/BACKLOG-FINDING-001.md`
(FINDING-001) — a real product/ingestion gap, not a query-technique problem.
No amount of Cypher cleverness recovers text that was never extracted into
the graph.
