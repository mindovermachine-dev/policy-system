---
name: ps-policy-lifecycle
description: Create a draft Policy, read a Policy's full Standard/Control tree, and move a Policy through its governance lifecycle (propose, approve, reject, revert-to-draft) via PS Service's Policy Lifecycle component.
---

# ps-policy-lifecycle

## Purpose

Create a new draft `Policy`, read a `Policy`'s current status and its full
`Standard`/`Control` tree, and move a `Policy` through its
`draft` → `proposed` → `approved` → `deprecated` governance workflow — the
MCP-tool-backed front end to PS Service's `ps.service.policylifecycle`
component (issue #134).

**Scope:** six tools — `create-policy-draft`, `get-policy`,
`propose-policy`, `approve-policy`, `reject-policy`,
`revert-policy-to-draft`. Editing or amending an existing `Policy`'s
content, and forking a new version linked to a prior approved one via
`SUPERSEDED_BY`, are issue #136's own not-yet-built tooling — out of scope
here; do not attempt either with these tools.

**Deliverable:** the created/read `Policy` (and its `Standard`/`Control`
tree, where the tool returns one), or the lifecycle action's own
confirmation — or the specific named error state on failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose a `create-policy-draft` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the `ps-mcp` connector.

## Core Principles

- Confirm with the user which `Policy` (by `policy_id`, or by the exact
  `title` before one exists) and which lifecycle action they want, and that
  they want to proceed, before calling anything beyond `get-policy` —
  `create-policy-draft`/`propose-policy`/`approve-policy`/`reject-policy`/
  `revert-policy-to-draft` are all real, permanent, audited actions with
  real governance consequences. `get-policy` is a plain read and needs no
  such confirmation.
- Before calling `create-policy-draft`, help the user think through the
  Policy's eventual structured content against
  `ps-skills/ps-plugin/rubrics/policy-rubric.md`/`policy-template.md`
  (one scored criterion / one section per field: `scope_in`/`scope_out`,
  `normative_commitments`, `review_cadence`, `exception_pathway`,
  `measurable_outcomes`, `capability_grouping_rationale`, per
  `docs/artifacts/ps-domain-concepts.md`'s Policy properties table) and any
  `Standard`/`Control` children it will need against
  `standard-rubric.md`/`standard-template.md` and
  `control-rubric.md`/`control-template.md` — `create-policy-draft` accepts
  an optional `standards` argument (each with its own optional `controls`),
  so Standard/Control children can be attached at creation time, in the
  same call. There is still no separate "add standard"/"add control" tool
  to attach children to an _existing_ Policy afterwards — plan the
  Standard/Control tree before calling `create-policy-draft` and pass it
  all in one call; a Policy created without any `standards` still has zero
  Standards and cannot satisfy `propose-policy`'s completeness gate (below)
  unless a later `create-policy-draft` call for a _different_ title
  includes them (there is no way to add children to this same Policy
  after the fact through this tool surface).
- Never fabricate a `policy_id`, `status`, `version`, `owner_subject`/
  `owner_issuer`, or any `standard`/`control` field — report exactly what
  the tool returned.
- Never guess or default a `policy_id` the user hasn't named — if they
  don't already know it, ask them (there is no roster/list tool for
  Policies in this skill's scope).
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Identify the target.** For `get-policy`/`propose-policy`/
   `approve-policy`/`reject-policy`/`revert-policy-to-draft`, get the exact
   `policy_id` from the user. For `create-policy-draft`, get the exact
   `title` to use (the Policy's id is derived deterministically from it —
   calling `create-policy-draft` again with the same title fails with the
   duplicate-title error below, not a second Policy), plus any `Standard`/
   `Control` children (each Standard a `title` and optional `controls`,
   each Control a `title` and optional `control_type`) the user wants
   attached at creation.
2. **Confirm before an effectful call.** For every tool except `get-policy`,
   confirm the exact target and action with the user, and that they want to
   proceed, before calling the tool.
3. **Know who can do what, and from which status, before calling:**

   | Action                   | Who                                                                                                                                       | Required current status | Resulting status                                                                                                                                                                     |
   | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
   | `create-policy-draft`    | any authenticated caller (becomes the owner)                                                                                              | — (new Policy)          | `draft`                                                                                                                                                                              |
   | `get-policy`             | Draft: owner, or a caller holding `SystemOwner`/`SystemAdmin`. Proposed/Approved/Deprecated: any authenticated caller.                    | any                     | unchanged (read-only)                                                                                                                                                                |
   | `propose-policy`         | owner only (no role required)                                                                                                             | `draft`                 | `proposed`                                                                                                                                                                           |
   | `approve-policy`         | a caller holding `PolicyManager`, and never the Policy's own owner (self-approval always blocked, even for a `PolicyManager` who owns it) | `proposed`              | `approved` (and, if this Policy has an approved prior linked via `SUPERSEDED_BY`, that prior's whole tree auto-cascades to `deprecated` in the same call, as its own separate event) |
   | `reject-policy`          | same as `approve-policy` (`PolicyManager`, never the owner)                                                                               | `proposed`              | `draft`                                                                                                                                                                              |
   | `revert-policy-to-draft` | owner only — **not** role-gated; a `PolicyManager` who isn't the owner is rejected exactly like any other non-owner                       | `proposed`              | `draft`                                                                                                                                                                              |

   Every action's cascade applies to the Policy **and every Standard/Control
   in its tree** in one write. `approve-policy`/`reject-policy` require the
   caller and owner to differ in _either_ subject or issuer — the same
   subject under a different issuer is treated as a different person and
   may approve/reject (this also means it is never blocked as
   self-approval).

4. **Completeness gate (propose only):** `propose-policy` requires the
   Policy to have at least one `Standard` attached; a Standard with zero
   Controls is never checked. There is no minimum for approve/reject/revert
   beyond the status/ownership/role gates above.
5. **Local-test-bypass caveat:** under the local-test bypass, every one of
   these six tools treats the bypass as a real authenticated caller, always
   resolving to the same fixed identity for both the acting caller and (for
   Policies it creates) the owner. Because `approve-policy`/`reject-policy`
   always compare the same fixed identity against itself, self-approval is
   always blocked under the bypass — **`approve-policy` and `reject-policy`
   are not exercisable under the local-test bypass**; only
   `create-policy-draft`/`get-policy`/`propose-policy`/
   `revert-policy-to-draft` can be meaningfully tested that way.
6. **Call the tool** — `create-policy-draft` (`title`, optional
   `standards`), `get-policy` (`policy_id`), `propose-policy` (`policy_id`),
   `approve-policy` (`policy_id`), `reject-policy` (`policy_id`), or
   `revert-policy-to-draft` (`policy_id`) — on the PS Service connector
   selected at On Load.
7. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic "action
   failed":

   | Tool result shape                                                                                                                               | Named state to report                                                                                                                                                                                                                                          | Applies to                                                                                  |
   | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or `error: this action requires a real authenticated caller (the local-test bypass counts as one)`                | `unauthenticated` — the caller has no real authenticated session (unlike access-role management, the local-test bypass DOES count as one here)                                                                                                                 | all six                                                                                     |
   | `error: the policy graph database is not reachable`                                                                                             | `policy_graph_unavailable` — the FalkorDB-backed policy graph cannot be reached                                                                                                                                                                                | all six                                                                                     |
   | `error: no Policy exists with id '<policy_id>'`                                                                                                 | `policy_not_found` — no Policy exists with the given id                                                                                                                                                                                                        | `get-policy`, `propose-policy`, `approve-policy`, `reject-policy`, `revert-policy-to-draft` |
   | `error: you do not have access to this Policy`                                                                                                  | `draft_access_denied` — the caller is neither the owner nor `SystemOwner`/`SystemAdmin` (for `get-policy` on a Draft) or is not the owner (for `propose-policy`/`revert-policy-to-draft`); never reveals whether the Policy exists, who owns it, or its status | `get-policy`, `propose-policy`, `revert-policy-to-draft`                                    |
   | `error: a Policy titled '<title>' already exists (id '<existing_policy_id>'); amend it via the supersede workflow, or choose a different title` | `title_already_exists` — the derived id for `title` collides with an existing Policy                                                                                                                                                                           | `create-policy-draft`                                                                       |
   | `error: standards[<i>]...` (or `standards[<i>].controls[<j>]...`)                                                                               | `malformed_standards_input` — the optional `standards` argument (or a nested Standard/Control entry) is shaped wrong: not a list of objects, a required `title` missing/empty, or an invalid `control_type`; names the exact offending position                | `create-policy-draft`                                                                       |
   | `error: at least one Standard is required before a Policy can be proposed`                                                                      | `incomplete_for_proposal` — the Policy has zero Standards attached                                                                                                                                                                                             | `propose-policy`                                                                            |
   | `error: cannot <action> a Policy in status '<current_status>' (requires status '<required_status>')`                                            | `invalid_status_transition` — the Policy is not currently in the status this action requires                                                                                                                                                                   | `propose-policy`, `approve-policy`, `reject-policy`, `revert-policy-to-draft`               |
   | `error: You do not have the required access role for this action.`                                                                              | `access_denied` — the caller does not hold `PolicyManager`                                                                                                                                                                                                     | `approve-policy`, `reject-policy`                                                           |
   | `error: you cannot approve or reject a Policy you own`                                                                                          | `self_approval_blocked` — the caller is the Policy's own owner; report this distinctly, it is a deliberate safety rejection, not a bug                                                                                                                         | `approve-policy`, `reject-policy`                                                           |
   | `error: The authorization store is temporarily unavailable.`                                                                                    | `authorization_store_unavailable` — the role-authorization store cannot be reached; role-gated actions fail closed until it is                                                                                                                                 | `approve-policy`, `reject-policy`                                                           |
   | `error: an unexpected error occurred`                                                                                                           | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                              | all six                                                                                     |
   | Successful structured response                                                                                                                  | Report the created/read Policy or the transition's own confirmation                                                                                                                                                                                            | all six                                                                                     |

## Output

In this shape on success:

For `create-policy-draft`:

```text
Created draft Policy: <policy_id> — "<title>"
  Status: draft · Version: <version> · Owner: <owner_subject>
```

For `get-policy`:

```text
Policy <policy_id> — "<title>"
  Status: <status> · Version: <version> · Owner: <owner_subject> (<owner_issuer>)

  Standards:
    <standard_id> — "<title>" (<status>)
      Controls:
        <control_id> — "<title>" [<control_type>] (<status>)
      ...
    ...
```

For `propose-policy`/`approve-policy`/`reject-policy`/
`revert-policy-to-draft`:

```text
<Proposed|Approved|Rejected|Reverted>: <policy_id>, now <status>
  Standards affected: <standard_ids>
  Controls affected: <control_ids>
```

`approve-policy` additionally reports, when non-null:

```text
  Auto-deprecated prior version: <auto_deprecated_policy_id>
```

On a named error state, report that state plainly instead — do not emit an
Output block that implies a successful action when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never call `create-policy-draft`, `propose-policy`, `approve-policy`,
  `reject-policy`, or `revert-policy-to-draft` without the user having
  named the exact target (`title` or `policy_id`) and confirmed they want
  to proceed.
- Never collapse the named error states into each other or into a generic
  message.
- Never fabricate a `policy_id`, `title`, `status`, `version`,
  `owner_subject`/`owner_issuer`, `standard_ids`/`control_ids`, or
  `auto_deprecated_policy_id` value that the tool did not actually return.
- Never suggest amending an existing Policy's content or linking it to a
  successor version via these tools — that is issue #136's scope, not
  this skill's.
