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
`revert-policy-to-draft`. `create-policy-draft` also forks a new version
linked to a prior approved Policy via `supersedes_policy_id`
(`SUPERSEDED_BY`). Editing an existing draft's content is a separate
tool family (`update-policy-draft` and its Standard/Control siblings), out
of scope here.

**Capability governance (`GOVERNED_BY`):** a fresh draft created with
`capability_ids` claims those Capabilities at creation — each gets a
`GOVERNED_BY` edge to the new draft right away, and keeps it until the
draft is approved or abandoned. A fork never claims Capabilities at
creation (`capability_ids` is ignored when `supersedes_policy_id` is set):
the Capabilities governed by the superseded Policy stay on it until the
fork is approved, then move to the fork in the same operation as the
status change (`approve-policy` reports them as `governed_capability_ids`).
Every Capability has exactly one governing Policy throughout.

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
unreachable (see `references/error-states.md`) rather than
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
3. **Know who can do what, and from which status, before calling.** For `create-policy-draft` and `get-policy`, no further rules are needed. Before calling `propose-policy`, `approve-policy`, `reject-policy` or `revert-policy-to-draft`, read `references/transition-rules.md` and follow it: it holds the who/status/resulting-status table, the cascade and approver-differs rules, and the propose completeness gate (at least one `Standard`).
4. **Local-test-bypass caveat:** if the PS Service connector is running under the local-test bypass, read `references/local-test-bypass.md` first -- `approve-policy` and `reject-policy` are not exercisable under it.
5. **Call the tool** — `create-policy-draft` (`title`, optional
   `standards`, optional `capability_ids`, optional `supersedes_policy_id`), `get-policy` (`policy_id`), `propose-policy` (`policy_id`),
   `approve-policy` (`policy_id`), `reject-policy` (`policy_id`), or
   `revert-policy-to-draft` (`policy_id`) — on the PS Service connector
   selected at On Load.
6. **Report the result.** On a successful structured response, report the created/read Policy or the transition's own confirmation. On anything else, read `references/error-states.md` and report exactly one of its named states -- never collapsed into each other or into a generic "action failed"; an unrecognised failure is reported as an unexpected error, never guessed at.

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

On a successful `propose-policy`/`approve-policy`/`reject-policy`/`revert-policy-to-draft`, or when `create-policy-draft` returns a non-empty `governed_capability_ids`, read `references/transition-output.md` and use its shapes (including `approve-policy`'s auto-deprecated prior and governed Capabilities lines).

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
- Never suggest amending an existing Policy's content through these tools
  — content edits belong to the `update-*-draft` tools. To amend an
  approved Policy, fork it with `create-policy-draft` and
  `supersedes_policy_id`; never pass `capability_ids` with it.
- Never claim a Capability that already has a governing Policy, and never
  describe a `governance_conflict` approval as applied.
