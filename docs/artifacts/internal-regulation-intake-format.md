# Internal-Regulation Intake Format

> **Status: Draft.** This specifies the target input format for `ps-cli internal ingest`,
> a deliverable of [#54](https://github.com/mindovermachine-dev/policy-system/issues/54)
> (not yet implemented). Once #54 ships, `ps-cli` and `ps-service` will validate submitted
> files against this format directly. Treat this document as the format's authoritative
> specification-in-progress, not as something you can ingest today.

## Who this is for

You are a **Policy Manager** preparing one of your organization's internal policies,
standards, or SoPs (e.g. an Engineering Practices standard) for ingestion into the
compliance knowledge graph via `ps-cli internal ingest`. This document tells you — or the
AI assistant you use to do the extraction — exactly what JSON to produce.

This format is deliberately independent of `docs/artifacts/ps-domain-concepts.md`, which is
our own internal reference for the graph's ontology and identity-hashing rules. You do not
need to read that document or reproduce its id formats. Your JSON uses **your own arbitrary
local ids**; PS Service mints the real canonical ids when it ingests your file.

## Using this document with an AI assistant

This format is designed to be handed to an LLM chat assistant (ChatGPT, Claude, Copilot, or
similar) alongside your actual policy source material — Word docs, wiki pages, PDFs, slide
decks, whatever you have — so the assistant does the extraction work for you. You do not need
to hand-write the JSON yourself.

**Suggested prompt**, with this document and your source material both attached or pasted in:

> I'm attaching our internal policy/standard documents. Using the JSON format specification
> attached, extract:
> - every distinct **Role** (a type of person/team the policy assigns duties to),
> - every distinct **Requirement** (a specific "shall"/"must"/"should" statement, with its
>   section or clause reference),
> - the **Obligation** each Requirement creates (a duty, attributed to exactly one Role),
> - the technical or organizational **Capability** each Obligation requires.
>
> Produce a single JSON file that follows the format's node/edge shape exactly. Use short,
> readable local ids of your own choosing (e.g. `"role-1"`, `"obl-least-privilege"`) — never
> invent ids that look like our internal `role_..._<hash>` format, that's not what's expected
> here. Reuse the same Capability id whenever two Obligations require conceptually the same
> capability, even if worded differently in the source text.

**Review before ingesting.** Treat the assistant's output as a first draft, not a final
answer — the same way our own extraction pipeline's output gets reviewed. In particular,
check:
- every Obligation is linked to **exactly one** Role via `HAS` (see [Authoring rules](#authoring-rules-the-model-must-follow)),
- Capabilities are genuinely reused (not re-minted with a slightly different name) when two
  Obligations describe the same underlying capacity,
- `source_ref` values on `DEFINES`/`EXPRESSES` edges point at real, checkable locations in
  your source document.

## Format overview

A submission is a single JSON object with two top-level arrays: `nodes` and `edges`. There is
no `graph_name` or other target-graph field — PS Service determines where your data is stored
from your `RegulatoryInstrument` node's own `id`, so there is nothing to get wrong there.

```json
{
  "nodes": [
    { "label": "...", "id": "...", "properties": { ... } }
  ],
  "edges": [
    { "type": "...", "from": { "label": "...", "id": "..." }, "to": { "label": "...", "id": "..." }, "properties": { ... } }
  ]
}
```

Only eight node labels and eight edge types are recognized. Anything else is rejected — no
partial file is ever accepted.

| Allowed node labels | Allowed edge types |
|---|---|
| `RegulatoryInstrument`, `Role`, `Requirement`, `Obligation`, `Capability`, `Policy`, `Standard`, `Control` | `DEFINES`, `EXPRESSES`, `HAS`, `SATISFIED_BY`, `REQUIRES`, `GOVERNED_BY`, `SUPPORTED_BY`, `IMPLEMENTED_BY` |

`Policy`, `Standard`, and `Control` are **authored by you**, not derived — see the
[`Policy`](#policy), [`Standard`](#standard), and [`Control`](#control) node references and the
[`GOVERNED_BY`](#edge-reference)/[`SUPPORTED_BY`](#edge-reference)/
[`IMPLEMENTED_BY`](#edge-reference) edges below. Do **not** include `PracticeArea` or `RiskPath`
nodes — those are not yet supported by this format; including them is a schema violation.

## Ids: yours are local, ours are canonical

Every node in your file needs an `id` — but except for `RegulatoryInstrument`, **that id is
never stored**. It exists only so your file's own `edges` can reference your own nodes. PS
Service mints its real, permanent id for each node when it ingests your file, using its own
content-derived formulas, and rewrites every edge to point at the new id automatically.

| Label | Your `id` | What PS Service actually stores |
|---|---|---|
| `RegulatoryInstrument` | Your own natural key, e.g. `ENGPRAC-3.0` (`{SHORT}-{VERSION}`). **Used as-is** — pick something unique and stable, since re-ingesting under a *different* id creates a second instrument rather than updating this one. | The same id, unchanged |
| `Role` | Any local string, e.g. `"role-1"` or `"role-security-engineer"` | Minted from the Role's own `name` |
| `Requirement` | Any local string, e.g. `"req-1"` | Minted from your instrument's id plus this Requirement's `EXPRESSES` edge's `source_ref` |
| `Obligation` | Any local string, e.g. `"obl-1"` | Minted from the Obligation's own `text` plus the Role that bears it |
| `Capability` | Any local string, e.g. `"cap-1"` | Minted from the Capability's own `name` alone |
| `Policy` | Any local string, e.g. `"pol-1"` | Minted from the Policy's own `title` alone |
| `Standard` | Any local string, e.g. `"std-1"` | Minted from the Policy it supports plus this Standard's own `title` |
| `Control` | Any local string, e.g. `"ctrl-1"` | Minted from the Standard it verifies plus this Control's own `title` |

Because Capability ids are minted from `name` alone, **reuse the same local id** across every
Obligation that requires the same underlying capability — that's what lets one Capability
converge duties from different parts of your document (or even different documents) onto one
node, instead of minting a near-duplicate for each.

## Node reference

### `RegulatoryInstrument` (exactly one per file)

| Property | Required | Notes |
|---|---|---|
| `title` | Yes | |
| `source_type` | Yes | Must be exactly `"internal"` — anything else is rejected |
| `effective_date` | Yes | ISO 8601 date |
| `version` | Yes | String, e.g. `"3.0"` |
| `status` | Yes | `active` \| `superseded` \| `vacated` — use `active` for a first submission |
| `jurisdiction` | No | Optional for internal sources; use it for an org-unit scope if useful, or omit |

### `Role`

| Property | Required | Notes |
|---|---|---|
| `name` | Yes | e.g. `"Engineering Manager"` |
| `description` | No | |

### `Requirement`

| Property | Required | Notes |
|---|---|---|
| `text` | Yes | The exact "shall"/"must"/"should" statement |
| `type` | Yes | `requirement` \| `prohibition` \| `recommendation` |
| `status` | No | `active` \| `deprecated` — default `active` |

### `Obligation`

| Property | Required | Notes |
|---|---|---|
| `text` | Yes | The duty in your own words, e.g. `"Enforce least-privilege access"` |

### `Capability`

| Property | Required | Notes |
|---|---|---|
| `name` | Yes | e.g. `"Access Control & Authentication"` |
| `description` | No | |
| `type` | No | e.g. `technical`, `organizational` |

`confidence` (a 0.0–1.0 float our own LLM extraction records on every `Role`/`Requirement`/
`Obligation`/`Capability` node) is **not required** from you and defaults to full confidence
(`1.0`) when omitted — it exists to record an extracting model's uncertainty, and your content
is either your own authored policy or your own assistant's best-effort extraction of it, not
our pipeline's extraction.

### `Policy`

Unlike `Role`/`Requirement`/`Obligation`/`Capability` above, a `Policy` is not extracted from
your source material at all — it is a governance node you author directly, representing an
organizational commitment your Capabilities are governed by.

| Property | Required | Notes |
|---|---|---|
| `title` | Yes | e.g. `"Access Control Policy"` |
| `status` | Yes | `draft` \| `approved` \| `deprecated` — use `draft` for a first submission |
| `description` | No | |
| `owner_id` | No | An identifier for the person/team accountable for this Policy, if useful |
| `version` | No | |

`Policy` never carries a `confidence` property — there is no extracting model's uncertainty to
record for a node you authored yourself, so this schema does not accept one on `Policy` at all
(submitting one is a schema violation, not a silently-dropped field).

### `Standard`

Like `Policy`, a `Standard` is not extracted from your source material — it is a governance
node you author directly, representing the implementation guidance (procedures, technical
specifications, testing expectations) that turns one of your Policies into something concrete
enough to build and verify. Every `Standard` must support exactly one `Policy` (see
[`SUPPORTED_BY`](#edge-reference) below) — a Policy commonly has several Standards under it.

| Property | Required | Notes |
|---|---|---|
| `title` | Yes | e.g. `"Access Control Standard"` |
| `implementation_status` | Yes | `draft` \| `implemented` \| `reviewed` \| `deprecated` — use `draft` for a first submission |
| `description` | No | |
| `version` | No | |

`Standard` never carries a `confidence` property, for the same reason as `Policy` — submitting
one is a schema violation, not a silently-dropped field.

### `Control`

Like `Policy` and `Standard`, a `Control` is not extracted from your source material — it is a
governance node you author directly, representing a concrete, testable verification mechanism
confirming that a Standard's procedure is actually being followed (an automated check or a
manual review). Every `Control` must implement exactly one `Standard` (see
[`IMPLEMENTED_BY`](#edge-reference) below) — a Standard commonly has several Controls under it.

| Property | Required | Notes |
|---|---|---|
| `type` | Yes | `automated` \| `manual` |
| `title` | Yes | e.g. `"Automated Log Retention Integrity Check"` |
| `implementation_status` | Yes | `planned` \| `implemented` \| `reviewed` \| `deprecated` — use `planned` for a first submission |
| `description` | No | |
| `execution_frequency` | No | e.g. `"daily"`, `"quarterly"` |
| `last_test_date` | No | ISO 8601 date |
| `next_review_date` | No | ISO 8601 date |
| `evidence_ref` | No | An opaque pointer into your evidence/audit store |

`Control` never carries a `confidence` property, for the same reason as `Policy`/`Standard` —
submitting one is a schema violation, not a silently-dropped field.

## Edge reference

| Edge | From → To | Required properties | Cardinality |
|---|---|---|---|
| `DEFINES` | `RegulatoryInstrument` → `Role` | `source_ref` (string — where in your document this Role is defined/named) | one instrument to many roles |
| `EXPRESSES` | `RegulatoryInstrument` → `Requirement` | `source_ref` (string — the clause/section reference, e.g. `"4.1"`) | one instrument to many requirements |
| `HAS` | `Role` → `Obligation` | — | **exactly one** Role per Obligation |
| `SATISFIED_BY` | `Requirement` → `Obligation` | — | many-to-many |
| `REQUIRES` | `Obligation` → `Capability` | — | many-to-many |
| `GOVERNED_BY` | `Capability` → `Policy` | — (no required properties) | **at most one** Policy per Capability |
| `SUPPORTED_BY` | `Policy` → `Standard` | — (no required properties) | **exactly one** Policy per Standard |
| `IMPLEMENTED_BY` | `Standard` → `Control` | — (no required properties) | **exactly one** Standard per Control |

### Authoring rules the model must follow

- Every node your edges reference must actually exist in `nodes` — a `from`/`to` pointing at
  an id you never declared is rejected, and nothing partial is written.
- Every `Obligation` must have **exactly one** inbound `HAS` edge. An Obligation with zero or
  more than one bearing Role is invalid.
- A `Requirement` can be `SATISFIED_BY` more than one `Obligation`, and the same `Obligation`
  can satisfy more than one `Requirement` — that's expected when one duty recurs across
  several clauses.
- An `Obligation` can `REQUIRES` more than one `Capability`, and the same `Capability` can be
  `REQUIRES`d by many `Obligation`s — this is the intended convergence point (see
  [Ids](#ids-yours-are-local-ours-are-canonical) above).
- A `Capability` can have **at most one** outbound `GOVERNED_BY` edge to a `Policy`. Governance
  is optional per-Capability — a Capability with zero `GOVERNED_BY` edges is valid — but a
  Capability with two or more is rejected.
- Every `Standard` must have **exactly one** inbound `SUPPORTED_BY` edge from a `Policy`. A
  Standard with zero or more than one supporting Policy is invalid.
- Every `Control` must have **exactly one** inbound `IMPLEMENTED_BY` edge from a `Standard`. A
  Control with zero or more than one implemented Standard is invalid.

## Worked example

A small slice — one instrument, two roles, two requirements, two obligations, two
capabilities — showing every regulatory-spine edge type once, plus a full
`Policy → Standard → Control` governance chain authored against one of the Capabilities. A real
submission simply repeats this pattern.

```json
{
  "nodes": [
    {
      "label": "RegulatoryInstrument",
      "id": "ENGPRAC-3.0",
      "properties": {
        "title": "Engineering Practices Policy",
        "source_type": "internal",
        "effective_date": "2026-08-01",
        "version": "3.0",
        "status": "active"
      }
    },
    { "label": "Role", "id": "role-eng-manager", "properties": { "name": "Engineering Manager", "description": "Owns team execution and adherence to engineering policy." } },
    { "label": "Role", "id": "role-security-engineer", "properties": { "name": "Security Engineer", "description": "Implements and verifies security controls across the SDLC." } },

    { "label": "Requirement", "id": "req-1", "properties": { "text": "Engineering managers shall maintain approved policy governance and controlled exception handling for all production services.", "type": "requirement", "status": "active" } },
    { "label": "Requirement", "id": "req-2", "properties": { "text": "Engineering systems shall enforce strong authentication, least privilege, and periodic access review.", "type": "requirement", "status": "active" } },

    { "label": "Obligation", "id": "obl-policy-governance", "properties": { "text": "Maintain approved policy governance and controlled exception handling" } },
    { "label": "Obligation", "id": "obl-access-control", "properties": { "text": "Enforce strong authentication, least privilege, and periodic access review" } },

    { "label": "Capability", "id": "cap-policy-exception-governance", "properties": { "name": "Policy Exception Governance" } },
    { "label": "Capability", "id": "cap-access-control", "properties": { "name": "Access Control & Authentication" } },

    { "label": "Policy", "id": "pol-access-control", "properties": { "title": "Access Control Policy", "status": "approved" } },
    { "label": "Standard", "id": "std-access-control", "properties": { "title": "Access Control Standard", "implementation_status": "implemented" } },
    { "label": "Control", "id": "ctrl-access-review", "properties": { "type": "automated", "title": "Automated Access Review Check", "implementation_status": "implemented", "execution_frequency": "daily" } }
  ],
  "edges": [
    { "type": "DEFINES", "from": { "label": "RegulatoryInstrument", "id": "ENGPRAC-3.0" }, "to": { "label": "Role", "id": "role-eng-manager" }, "properties": { "source_ref": "Sec. 1" } },
    { "type": "DEFINES", "from": { "label": "RegulatoryInstrument", "id": "ENGPRAC-3.0" }, "to": { "label": "Role", "id": "role-security-engineer" }, "properties": { "source_ref": "Sec. 1" } },

    { "type": "EXPRESSES", "from": { "label": "RegulatoryInstrument", "id": "ENGPRAC-3.0" }, "to": { "label": "Requirement", "id": "req-1" }, "properties": { "source_ref": "4.1" } },
    { "type": "EXPRESSES", "from": { "label": "RegulatoryInstrument", "id": "ENGPRAC-3.0" }, "to": { "label": "Requirement", "id": "req-2" }, "properties": { "source_ref": "4.3" } },

    { "type": "HAS", "from": { "label": "Role", "id": "role-eng-manager" }, "to": { "label": "Obligation", "id": "obl-policy-governance" } },
    { "type": "HAS", "from": { "label": "Role", "id": "role-security-engineer" }, "to": { "label": "Obligation", "id": "obl-access-control" } },

    { "type": "SATISFIED_BY", "from": { "label": "Requirement", "id": "req-1" }, "to": { "label": "Obligation", "id": "obl-policy-governance" } },
    { "type": "SATISFIED_BY", "from": { "label": "Requirement", "id": "req-2" }, "to": { "label": "Obligation", "id": "obl-access-control" } },

    { "type": "REQUIRES", "from": { "label": "Obligation", "id": "obl-policy-governance" }, "to": { "label": "Capability", "id": "cap-policy-exception-governance" } },
    { "type": "REQUIRES", "from": { "label": "Obligation", "id": "obl-access-control" }, "to": { "label": "Capability", "id": "cap-access-control" } },

    { "type": "GOVERNED_BY", "from": { "label": "Capability", "id": "cap-access-control" }, "to": { "label": "Policy", "id": "pol-access-control" } },
    { "type": "SUPPORTED_BY", "from": { "label": "Policy", "id": "pol-access-control" }, "to": { "label": "Standard", "id": "std-access-control" } },
    { "type": "IMPLEMENTED_BY", "from": { "label": "Standard", "id": "std-access-control" }, "to": { "label": "Control", "id": "ctrl-access-review" } }
  ]
}
```

## What gets rejected

- A node label or edge type outside the allow-list above.
- A `RegulatoryInstrument` whose `source_type` is not `"internal"`.
- An edge whose `from` or `to` id is not declared by any node in `nodes`.
- A `Policy`, `Standard`, or `Control` node carrying a `confidence` property.
- A Capability with two or more outbound `GOVERNED_BY` edges.
- A Standard with zero, or two or more, inbound `SUPPORTED_BY` edges.
- A Control with zero, or two or more, inbound `IMPLEMENTED_BY` edges.
- Any `PracticeArea`/`RiskPath` content — not yet supported by this format.

Every rejection is fail-closed: no partial graph is ever written from an invalid file.

## Versioning

This document and its eventual companion JSON Schema are versioned together. A breaking
change to the node/edge shape above will be called out with a new version number once #54
ships and the schema file exists; this document will link to it directly at that point.

## See also

- [`ps-domain-concepts.md`](./ps-domain-concepts.md) — the full internal ontology this format
  is a customer-facing subset of, for anyone who wants the underlying rationale.
- [`user-guide.md`](./user-guide.md) — end-to-end `ps-cli` usage, including how to point
  `ps-cli` at the right environment before running `internal ingest`.
- [Issue #54](https://github.com/mindovermachine-dev/policy-system/issues/54) — tracks the
  implementation this format specifies.
