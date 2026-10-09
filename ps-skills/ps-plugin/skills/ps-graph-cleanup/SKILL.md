---
name: ps-graph-cleanup
description: Compliance Officer graph cleanup — find groups of near-duplicate Capabilities and duplicate Obligations (same Role) in the Policy System compliance graph, and merge two Capabilities (with an explicit acknowledgment when exactly one has a governing policy), merge two Obligations under the same Role, release a Capability from a draft governing policy, or unmerge a Capability or Obligation merge, after a human decision, a preview and a passkey approval. Requires an explicit ComplianceOfficer grant.
---

# ps-graph-cleanup

## Purpose

The ingestion pipeline leaves duplicates in the compliance graph (for example
several near-identical "Vulnerability Remediation" capabilities). Deciding that
two nodes are the same duty is a judgement about regulatory meaning, so it is a
human decision by a Compliance Officer. This skill walks that person from
candidate discovery through a merge, a release or an unmerge to a recorded outcome.

**Scope:** capability candidate discovery
(`find-capability-merge-candidates`), duplicate-obligation discovery
(`find-duplicate-obligations`), merging two Capabilities (`merge-capabilities`; when both
have a governing policy it must be the **same** policy), merging two Obligations under the
**same Role** (`merge-obligations`), releasing a Capability from a **draft** governing policy
(`release-capability-governance`), reversing a Capability or Obligation merge from its audit snapshot (`unmerge`),
and `check-cleanup-approval` for any of them.
Discovery is read-only: it changes nothing and creates no approval.

**Deliverable:** for a merge, the preview the tool returned, the approval link, and the
outcome once the officer has signed (or the specific named error state). For
discovery, every candidate group the tool returned — for capabilities each
member's `id`, `name`, obligation count and governing policy plus the group's
evidence `basis` and merge case; for obligations each member's `id`, `text` and
its requirements' `source_ref`s plus the group's role and `basis` — or the
specific named error state.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"), the
connector the Policy System Plugin declares, pointing at a hosted PS Service.
Claude lists it as `plugin:ps-plugin:ps-mcp`. A connector that does not expose a
`find-capability-merge-candidates` tool is not a PS Service connector, whatever
it is named — report it as unreachable rather than proceeding against it.

## Core Principles

- The tool needs an explicit `ComplianceOfficer` grant on a real authenticated
  session. There is no admin override: SystemOwner and SystemAdmin are denied
  unless they were also granted `ComplianceOfficer`. The local-test bypass never
  satisfies it. Do not suggest a workaround; tell the user which grant is missing.
- A candidate group is a suggestion, not a verdict. Never merge or recommend a
  merge on the strength of a group alone; the Compliance Officer decides.
- Never fabricate a group, member or field — report exactly what the tool
  returned, every group, not a subset.
- Never silently retry a failed call — report the named failure and stop.

## Process

### Finding capability merge candidates

1. **Call the tool.** Invoke `find-capability-merge-candidates` on the PS Service
   connector selected at On Load. This is a read-only call. Pass no arguments to
   use the default similarity; pass `min_similarity` (greater than 0.5, at most
   1.0) only when the user asks for a stricter or looser sweep. When omitted, the
   configured Company Merge threshold applies, else 0.90. The tool compares the
   cached embedding already stored on each Capability; it never fetches or
   computes one, so a Capability with no cached embedding can only be found by
   name.
2. **Report the result**, distinguishing every non-success outcome into one of
   the following named states — never collapsed into a generic "discovery failed":

   | Tool result shape                                                  | Named state to report                                                                                    |
   | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure                                       | "PS Service is unreachable"                                                                              |
   | `error: graph cleanup requires a real authenticated caller`        | "Not signed in with a real session" — graph cleanup is never available under the local-test bypass       |
   | `error: You do not have the required access role for this action.` | "ComplianceOfficer grant missing" — ask a SystemOwner or SystemAdmin to grant it; no override exists     |
   | `error: The authorization store is temporarily unavailable.`       | "Authorization store unavailable" — the call was denied, fail-closed; try again later                    |
   | `error: the policy graph database is not reachable`                | The compliance graph database cannot be reached — distinct from a PS Service transport failure           |
   | `error: an unexpected error occurred`                              | An unrecognised failure — report it as unexpected; never guess at its cause                              |
   | Successful structured response                                     | Report every group plainly, including an explicit "no candidate groups" statement when `groups` is empty |

3. **Output**, in this shape on success:

   ```text
   Candidate groups: <count>

     group <n> (basis: <basis>, merge case: <1|2|3>)
       <id>  <name>  obligations: <count>  policy: <id> "<title>" (<status>) | none
       ...
   ```

   Each member carries `obligation_count` (the Obligations that require that
   Capability) and `governing_policy` (`id`, `title`, `status`, or `null` when
   ungoverned). The group's `merge_case` tells the Compliance Officer what a
   merge would involve:

   | `merge_case` | Meaning                                     | What it implies                                                                                                                            |
   | ------------ | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
   | 1            | No member has a governing policy            | A merge changes no policy coverage                                                                                                         |
   | 2            | Exactly one member has a governing policy   | A merge changes which capability that policy governs; it will need an explicit acknowledgment                                              |
   | 3            | Two or more members have governing policies | If `policies_distinct` is `true` the policies differ and a merge is blocked until governance is released; if `false` they share one policy |

   Report `merge_case` and `policies_distinct` exactly as returned; they are
   advisory, computed over the whole group rather than a specific pair.

   `basis` is `name` when every member's name is equal once case and punctuation
   are ignored, and `embedding` when at least one link between members is
   embedding similarity (a looser, advisory signal: read the names before
   treating a group as a duplicate).

### Finding duplicate obligations

1. **Call the tool.** Invoke `find-duplicate-obligations` on the same connector.
   Read-only. Pass no arguments to sweep every Role, or `role_id` to limit the
   sweep to one Role. The tool only ever groups Obligations under the **same
   role**: identical wording under two different Roles is two distinct duties
   (for example an importer's and a manufacturer's notification duty) and is never
   offered as a duplicate.
2. **Report the result** using the same named error states as the table above
   (the gate and graph errors are identical). On success report every group.
3. **Output**, in this shape on success:

   ```text
   Duplicate obligation groups: <count>

     group <n> (role: <role_name> <role_id>, basis: <basis>)
       <id>  "<text>"
         <requirement_id>  <source_ref>
         ...
   ```

   `basis` is `identical_text` when every member's text is equal once case and
   punctuation are ignored, and `near_text` when the members differ slightly
   (high word overlap; a looser, advisory signal: read the texts before treating
   a group as a duplicate). Each requirement line shows the `source_ref` of the
   article or paragraph the Requirement came from, so the Compliance Officer can
   check whether the regulation really states the duty twice. An Obligation with
   no requirements is shown without lines, which means it has no provenance.

### Merging two capabilities

For any pair of active Capabilities (merge case 1: neither governed, case 2: exactly one
governed, case 3: both governed by the **same** policy). Two Capabilities governed by
**different policies** cannot be merged; see the case 3 step below. The flow
is candidate -> preview -> confirm -> passkey -> result; never skip a step.

1. **Candidate.** The pair comes from `find-capability-merge-candidates` or from
   the Compliance Officer. A candidate group is a suggestion, never a verdict.
   Ask which Capability **survives** and which is **absorbed**: the absorbed one
   is kept as a `merged` tombstone pointing at the survivor, so the choice matters.
   Never choose for the user.
2. **Preview.** Call `merge-capabilities` with `survivor_id` and `absorbed_id`. This
   call never edits the graph. It returns `preview` (`edges_to_move` per class —
   `requires`, `covers`, `mitigated_by` —, `duplicate_edges_collapsed`,
   `obligations_affected`, `policy_case`, `governance`) plus `pending_approval_id`,
   `approval_url` and `expires_at`. Show every field plainly.
   - **Case 1** (`policy_case` 1, `governance` null): nothing more is needed.
   - **Case 2** (`policy_case` 2, exactly one side governed): the merge changes which
     capability the governing policy governs. The first call returns the `preview` with
     `acknowledgment_required: true` and a `message`, and creates **no approval**. Show the
     `governance` block plainly: the governing policy's `id`, `title` and `status`,
     `obligations_coverage_changed` (the obligations that newly gain that policy's coverage),
     the policy's governed set before and after, and the `message`. For an `approved` policy
     say exactly: its governed set changes, its content/version does not. Ask the officer to
     acknowledge the governance change in their own words. Only after an explicit "yes"
     repeat the call with `acknowledge_governance_change` set to true; that returns the
     `pending_approval_id` and `approval_url`, and the acknowledgment is part of what the
     passkey signs and of the audit row. Never set `acknowledge_governance_change` yourself,
     never infer it from the officer's earlier agreement to the merge, and never reuse it
     for another pair.
   - **Case 3, same policy** (`policy_case` 3, `governance.governed_side` `both`): both are
     already governed by one policy, so no acknowledgment is needed and the first call
     returns the approval. Say plainly that the absorbed capability leaves that policy's
     governed set and the survivor stays in it.
   - **Case 3, different policies**: the call returns an `error:` that names both policies
     and creates **no approval**. Pass the error text on verbatim. It points at
     `release-capability-governance` (release the absorbed capability from its policy
     first, which works only while that policy is a draft). If neither policy is a draft
     there is currently no completion path: a policy fork carries the whole governed set,
     so one capability cannot be dropped from an approved policy. Say so; do not invent a
     workaround and do not retry with an acknowledgment, which does not apply.
3. **Confirm.** Ask the Compliance Officer to confirm, in their own words, that
   these two are the same duty and that the survivor is right. Do not continue on
   silence or on your own judgement.
4. **Passkey.** Give the officer the `approval_url` to open in a browser and sign
   with a passkey (a first-time officer enrolls a passkey on that page). The
   approval is bound to this exact pair and to the previewed state, and is valid
   for 15 minutes. Never open, sign or complete it on the officer's behalf.
5. **Result.** Call `check-cleanup-approval` with the `pending_approval_id`. Report
   `status` (`pending`, `expired`, `signed`) and, once signed, `outcome`:
   `merged: true` means done; `error` carries the reason and means the graph was not
   changed by this approval; `reconciled: applied` means the merge was found in
   the graph after an interrupted run. If `pending`, say so and let the officer
   finish signing; never poll in a loop. If `expired`, start again from step 2.

Named states for `merge-capabilities`, never collapsed into each other:

| Tool result                                                                                      | Named state to report                                                                                                                                        |
| ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| the gate and graph errors listed in the discovery table above                                    | the same named states as discovery                                                                                                                           |
| `error: a capability cannot be merged into itself`                                               | "Same node" — pick two different Capabilities                                                                                                                |
| `error: the survivor capability does not exist` (or `absorbed`)                                  | "Capability not found" — check the id                                                                                                                        |
| `error: ... is a merged tombstone and cannot be merged`                                          | "Already merged" — a tombstone cannot be a side of a merge                                                                                                   |
| `error: ... is not active`                                                                       | "Not active" — only active Capabilities can be merged                                                                                                        |
| `error: cannot merge: capability ... is governed by policy ... and capability ... by policy ...` | "Different policies" — nothing was created; relay both policies and the release-governance step, or the no-completion-path statement when neither is a draft |
| `acknowledgment_required: true` (no approval created)                                            | "Acknowledgment needed" — present the governance change and ask; do not repeat the call until the officer agrees                                             |
| `error: the governance change was not acknowledged in this approval...` (an `outcome.error`)     | "Not acknowledged" — nothing changed; start again from the preview and ask the officer to acknowledge                                                        |
| `error: no pending approval with id ...`                                                         | "Approval not found" — wrong id, or it belongs to another officer                                                                                            |
| `outcome.error` of a signed approval                                                             | Report the message; the approval is spent, so a retry needs a new preview and a new passkey approval                                                         |

An `applied` audit row followed by a `failed` row for one approval id means no edit
occurred. Do not describe an approval as merged unless `check-cleanup-approval`
returned `merged: true` or `reconciled: applied`.

### Merging two obligations (same role)

Only for two Obligations under the **same role**; the tool rejects a pair spanning two
roles before creating any approval. The flow is candidate -> preview -> confirm ->
passkey -> result; never skip a step.

1. **Candidate.** The pair comes from `find-duplicate-obligations` or from the Compliance
   Officer. Ask which Obligation **survives** and which is **absorbed**. Unlike a
   Capability merge the absorbed Obligation is **deleted**, not kept as a tombstone, so
   say so plainly. Never choose for the user.
2. **Preview.** Call `merge-obligations` with `survivor_id` and `absorbed_id`. This call
   never edits the graph. It returns `preview` (`role_id` and `role_name`, `edges_to_move`
   with the `satisfied_by` requirement ids and `requires` capability ids that will union
   onto the survivor, `duplicate_edges_collapsed`, the absorbed side's
   `requirement_source_refs`) plus `pending_approval_id`, `approval_url` and `expires_at`.
   Show every field plainly, including each `source_ref`, so the officer can check the
   regulation really states the duty twice.
3. **Confirm.** Ask the Compliance Officer to confirm, in their own words, that these two
   are the same duty and that the survivor is right. Do not continue on silence or on your
   own judgement.
4. **Passkey.** Give the officer the `approval_url` to open in a browser and sign with a
   passkey. The approval is bound to this exact pair and to the previewed state, and is
   valid for 15 minutes. Never open, sign or complete it on the officer's behalf.
5. **Result.** Call `check-cleanup-approval` with the `pending_approval_id` and report it
   exactly as for a Capability merge (`merged: true` means done; `error` means the graph
   was not changed by this approval).

On execution the absorbed Obligation's `SATISFIED_BY` and `REQUIRES` edges union onto the
survivor, which keeps its single `HAS` role edge; the absorbed Obligation is deleted with
its full node and edge snapshot held in the `obligation.merge` audit row, and a
`MergedObligation` marker remains so a later ingest or restore that regenerates the
absorbed Obligation attaches to the survivor instead of recreating it. The marker is not an
Obligation and is never counted as one.

Named states for `merge-obligations`, never collapsed into each other:

| Tool result                                                      | Named state to report                                                                  |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| the gate and graph errors listed in the discovery table above    | the same named states as discovery                                                     |
| `error: an obligation cannot be merged into itself`              | "Same node" — pick two different Obligations                                           |
| `error: the survivor obligation does not exist` (or `absorbed`)  | "Obligation not found" — check the id; a previously merged id no longer exists         |
| `error: obligations under different roles cannot be merged: ...` | "Different roles" — an Obligation belongs to one Role; this pair can never be merged   |
| `error: the ... obligation ... is not borne by exactly one role` | "Role integrity" — report it as a data fault; never guess a Role                       |
| `outcome.error` of a signed approval                             | Report the message; the approval is spent, so a retry needs a new preview and approval |

### Releasing a capability from a policy

When the user wants to release a Capability from its draft governing policy (or a blocked different-policies merge points here), read `references/release-governance.md` and follow it. It covers the check -> preview -> confirm -> passkey -> result flow for `release-capability-governance` and its named states.

### Unmerging a merge

When the user wants to reverse a Capability or Obligation merge, read `references/unmerge.md` and follow it. It covers the locate -> preview -> confirm -> passkey -> result flow for `unmerge`, its conflicts and its named states.

## On-demand references

Read these only when the condition holds; each one is bound by the Guardrails below.

- When explaining what a merge involves per governance case, read `references/merge-case-reference.md`.
- When a tool returns an error or a signed approval has an `outcome.error` and the right named state is unclear, read `references/error-reference.md`.
- When two Capabilities are governed by two approved policies (no completion path), read `references/known-limitation.md`.
- When `check-cleanup-approval` reports a signed approval with no outcome or an interrupted run, read `references/reconciliation.md`.

## Guardrails

- The skill reaches PS Service exclusively through the `ps-mcp` connector —
  never a direct graph connection, a repo-local script or a spawned binary.
- Discovery, `merge-capabilities`, `merge-obligations`, `release-capability-governance` and `unmerge`
  never edit the graph themselves: a change happens only when the Compliance Officer signs the passkey
  approval. Do not
  offer Cypher or any other route to merge or delete nodes.
- Never merge a pair the Compliance Officer has not explicitly confirmed, never release a
  Capability from its policy or reverse a merge without the same confirmation, never
  acknowledge a governance change on their behalf, and never reuse an approval for a
  different pair.
- Never collapse the named error states in the tables into each other.
