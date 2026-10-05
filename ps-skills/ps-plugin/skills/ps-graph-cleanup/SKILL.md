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

`release-capability-governance` removes the `GOVERNED_BY` edge between one Capability and its
governing **draft** policy, so the Capability becomes ungoverned. It is the step the
different-policies merge error points at. It never edits a Policy, a Standard or a Control,
and it works only while the policy is a `draft`.

1. **Check.** Ask which Capability to release (the id; for a blocked merge, the absorbed one
   named in the error). Do not guess which policy; the tool derives it.
2. **Preview.** Call `release-capability-governance` with `capability_id`. This call never
   edits the graph. It returns `preview` (`capability_id`, `capability_name`, `policy_id`,
   `policy_title`, `policy_status`, the policy's governed set before and after) plus
   `pending_approval_id`, `approval_url` and `expires_at`. Show every field plainly and say
   that the Capability becomes ungoverned.
3. **Confirm.** Ask the Compliance Officer to confirm, in their own words, that this
   Capability should no longer be governed by that policy. Do not continue on silence or on
   your own judgement.
4. **Passkey.** Give the officer the `approval_url` to open in a browser and sign with a
   passkey. The approval is bound to this Capability, its policy and the previewed state,
   and is valid for 15 minutes. Never open, sign or complete it on the officer's behalf.
5. **Result.** Call `check-cleanup-approval` with the `pending_approval_id`. `released: true`
   means the edge was removed and the change was audited (`capability.release_governance`,
   with a before/after snapshot); `error` means the graph was not changed by this approval;
   `reconciled: applied` means the release was found in the graph after an interrupted run.

A policy that is not a draft is rejected before any approval exists:

- `proposed`: its owner can return it to draft with `revert-policy-to-draft` (see
  `ps-policy-lifecycle`), after which the release can be retried.
- `approved` or `deprecated`: point at the policy lifecycle (`ps-policy-lifecycle`) and say
  plainly what the error says: amending an approved policy is done by forking it, but a fork
  carries the whole governed set, so it does not by itself free this Capability. There is
  currently no completion path for releasing one Capability from an approved policy; do not
  invent a workaround, do not edit the graph another way and do not retry.

Named states for `release-capability-governance`, never collapsed into each other:

| Tool result                                                                                                          | Named state to report                                                                   |
| -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| the gate and graph errors listed in the discovery table above                                                        | the same named states as discovery                                                      |
| `error: the capability does not exist`                                                                               | "Capability not found" — check the id                                                   |
| `error: capability ... is a merged tombstone and cannot be released`                                                 | "Already merged" — a tombstone cannot be released                                       |
| `error: capability ... is not governed by any policy; nothing to release`                                            | "Not governed" — nothing to do                                                          |
| `error: release-capability-governance works only on a draft policy ...` and the policy is `proposed`                 | "Policy proposed" — relay the `revert-policy-to-draft` pointer                          |
| `error: release-capability-governance works only on a draft policy ...` and the policy is `approved` or `deprecated` | "Policy not a draft" — relay the lifecycle pointer and the no-completion-path statement |
| `outcome.error` of a signed approval                                                                                 | Report the message; the approval is spent, so a retry needs a new preview and approval  |

### Unmerging a merge

`unmerge` reverses a merge made with `merge-capabilities` or `merge-obligations`, from the audit
snapshot (`capability.merge` or `obligation.merge`) that merge recorded. It takes only
`merged_id`: the id that was absorbed (a `merged` Capability tombstone, or a deleted Obligation).
The tool works out which kind it is from the audit trail; `preview.kind` says so. The flow is
locate -> preview -> confirm -> passkey -> result; never skip a step.

1. **Locate.** Ask which absorbed id to unmerge (its `merged_id`). The tool finds the newest merge
   of that id that actually applied; do not guess a different id.
2. **Preview.** Call `unmerge` with `merged_id`. This call never edits the graph. It returns
   `preview` plus `pending_approval_id`, `approval_url` and `expires_at`. Show every field plainly
   and say what will happen.
   - **Capability** (`kind` `capability`): the preview has `merged_id`, `survivor_id`,
     `merge_approval_id`, `edges_to_restore` and `edges_removed_from_survivor` per class
     (`requires`, `covers`, `mitigated_by`, `governed_by`) and `survivor_added_edges`. The
     tombstone returns to `active` and its `MERGED_INTO` edge is removed; exactly the edges the
     merge moved off it come back from the snapshot (an edge the survivor already had before the
     merge stays on the survivor); edges added to the survivor since the merge stay in place and
     are listed in `survivor_added_edges`, so the officer can see what the survivor gained.
   - **Obligation** (`kind` `obligation`): the preview has `merged_id`, `merged_text`,
     `survivor_id`, `role_id`, `merge_approval_id`, `edges_to_restore` (`satisfied_by`,
     `requires`), `survivor_added_edges`, `survivor_edges_possibly_from_merge` and a `note`. The
     deleted Obligation is recreated under its original id with its properties and its `HAS` edge
     from the Role, its `SATISFIED_BY` and `REQUIRES` edges are restored exactly as the snapshot
     recorded them, and its `MergedObligation` marker is removed. The survivor's edges are never
     removed, because a union of edges cannot be attributed: edges added since the merge are in
     `survivor_added_edges`, and those in `survivor_edges_possibly_from_merge` may originate from
     the merge. Say so plainly; the officer may want to review them afterwards.
3. **Confirm.** Ask the Compliance Officer to confirm, in their own words, that this merge
   should be reversed. Do not continue on silence or on your own judgement.
4. **Passkey.** Give the officer the `approval_url` to open in a browser and sign with a
   passkey. The approval is bound to this merge and to the previewed state, and is valid for
   15 minutes. Never open, sign or complete it on the officer's behalf.
5. **Result.** Call `check-cleanup-approval` with the `pending_approval_id`. `unmerged: true`
   means the node is back and the change was audited (`capability.unmerge` or
   `obligation.unmerge`, with a before/after snapshot, the restored edges and
   `survivor_added_edges`); `error` means the graph was not changed by this approval;
   `reconciled: applied` means the unmerge was found in the graph after an interrupted run.

A conflict is rejected before any approval exists, with an explanation; pass it on verbatim.
There is no approval, and nothing is forced. Conflicts for a Capability include: the survivor
is no longer active (it was merged away or removed; the message names where it went), the
tombstone was re-pointed by a later merge (the message names the current target), the survivor's
governance changed since the merge so restoring the governing policy would contradict it, a
restored edge's endpoint no longer exists, or the Capability is not (or no longer) a merged
tombstone. Conflicts for an Obligation include: the Obligation already exists again, it has no
`MergedObligation` marker or the marker now points elsewhere, the survivor obligation no longer
exists (the message names where it went), the Role, a Requirement or a Capability endpoint is
gone, or a Capability endpoint is now a merged tombstone. If no merge of the id is in the audit
trail the tool says so. Do not invent a workaround, do not edit the graph another way and do not
retry.

Named states for `unmerge`, never collapsed into each other:

| Tool result                                                                                                                | Named state to report                                                                         |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| the gate and graph errors listed in the discovery table above                                                              | the same named states as discovery                                                            |
| `error: the audit trail could not be read right now; try again shortly`                                                    | "Audit trail unavailable" — nothing changed; try again later                                  |
| `error: no merge of ... was found in the audit trail; ...`                                                                 | "No merge to reverse" — check the id; only merges made with the cleanup tools can be unmerged |
| `error: capability ... is not a merged tombstone ...`                                                                      | "Not merged" — nothing to unmerge (never merged, or already unmerged)                         |
| `error: obligation ... already exists again; ...`                                                                          | "Obligation exists again" — nothing to unmerge                                                |
| `error: obligation ... has no MergedObligation marker ...`                                                                 | "No marker" — a conflict; report it, nothing is forced                                        |
| `error: the survivor capability ... is no longer active ...` or `error: the survivor obligation ... no longer exists ...`  | "Survivor changed" — a conflict; relay where it went; no approval exists                      |
| `error: capability ... was merged into ..., but its redirect now points at ...`                                            | "Redirect changed" — a conflict; relay the current target; no approval exists                 |
| `error: the survivor's governance changed since the merge ...`                                                             | "Governance conflict" — relay both policies; no approval exists                               |
| `error: ... no longer exists ...` for a Role, Requirement, Capability or edge endpoint, or `... is now a merged tombstone` | "Endpoint gone" — a conflict; relay the ids; no approval exists                               |
| `outcome.error` of a signed approval                                                                                       | Report the message; the approval is spent, so a retry needs a new preview and approval        |

## Merge case reference

One table for what `merge-capabilities` does in each governance case, so the officer is told the
same thing everywhere. The case is derived from the two Capabilities' governing policies
(`GOVERNED_BY`); for Obligations see the last row.

| Case                              | Situation                                     | What the first call does                                                                   | What the officer must do                                                                                                             |
| --------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| Case 1                            | Neither Capability has a governing policy     | Returns the preview and the approval                                                       | Confirm, then sign the passkey approval                                                                                              |
| Case 2                            | Exactly one Capability has a governing policy | Returns the preview only (`acknowledgment_required: true`), **no approval**                | Acknowledge the governance change in their own words; then repeat with `acknowledge_governance_change`; then sign                    |
| Case 3, same policy               | Both are governed by the **same** policy      | Returns the preview and the approval; no acknowledgment applies                            | Confirm, then sign; the absorbed Capability leaves the policy's governed set                                                         |
| Case 3, different policies        | Both are governed, by **different** policies  | Returns an `error:` naming both policies, **no approval**                                  | Release the absorbed Capability from a **draft** policy with `release-capability-governance`, then start again; see Known limitation |
| Obligations (`merge-obligations`) | Two Obligations under the **same Role**       | Returns the preview and the approval; a pair across two Roles is rejected, **no approval** | Confirm, then sign; the absorbed Obligation is deleted and a `MergedObligation` marker remains                                       |

In every case the graph changes only after the officer signs the passkey approval.

## Error reference

Every error a tool can return, in one place. Each row is its own named state; never collapse
them. The per-tool tables above give the exact wording for the preview-time rejections.

| Tool                               | Error or outcome                                                                                                                           | Named state and what to tell the officer                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| every tool                         | `error: graph cleanup requires a real authenticated caller`                                                                                | "Not signed in with a real session" (never available under the local-test bypass)           |
| every tool                         | `error: You do not have the required access role for this action.`                                                                         | "ComplianceOfficer grant missing" (no admin override)                                       |
| every tool                         | `error: The authorization store is temporarily unavailable.`                                                                               | "Authorization store unavailable" (denied, fail-closed)                                     |
| every tool                         | `error: the policy graph database is not reachable`                                                                                        | Graph database unreachable (distinct from a PS Service transport failure)                   |
| every tool                         | `error: an unexpected error occurred`                                                                                                      | Unexpected failure; never guess at its cause                                                |
| `find-capability-merge-candidates` | the gate and graph errors above                                                                                                            | Same named states; discovery changed nothing                                                |
| `find-duplicate-obligations`       | the gate and graph errors above                                                                                                            | Same named states; discovery changed nothing                                                |
| `merge-capabilities`               | rejected at preview (same node, not found, already merged, not active, different policies)                                                 | The per-tool named state; no approval exists                                                |
| `merge-obligations`                | rejected at preview (same node, not found, different roles, role integrity)                                                                | The per-tool named state; no approval exists                                                |
| `release-capability-governance`    | rejected at preview (not found, merged, not governed, policy not a draft)                                                                  | The per-tool named state; no approval exists                                                |
| `unmerge`                          | rejected at preview (no merge to reverse, not merged, a conflict, `error: the audit trail could not be read right now; try again shortly`) | The per-tool named state; no approval exists, nothing is forced                             |
| `check-cleanup-approval`           | `error: no pending approval with id ...`                                                                                                   | "Approval not found": wrong id, or it belongs to another officer                            |
| `check-cleanup-approval`           | `error: the approval status could not be settled right now; try again shortly`                                                             | "Status unavailable": try again shortly; nothing was changed by checking                    |
| any signed approval                | `outcome.error`: the graph changed since the preview; nothing was changed, ask for a new approval                                          | "Stale": start again from the preview                                                       |
| any signed approval                | `outcome.error`: ... could not be audited; nothing was changed                                                                             | "Not audited": the edit was refused because its audit row could not be written; start again |
| any signed approval                | `outcome.error`: ... could not be completed; check its status with check-cleanup-approval before retrying                                  | "Write failed": the approval is spent; check the status, then start again from the preview  |
| any signed approval                | `outcome.error`: this approval could not be executed; ask for a new approval                                                               | "Approval unusable": start again from the preview                                           |

An `outcome.error` always means the graph was not changed by that approval, except where the
message says to check the status first. Never describe an approval as done unless
`check-cleanup-approval` returned `merged: true`, `released: true`, `unmerged: true` or
`reconciled: applied`.

## Known limitation

Two Capabilities governed by two approved policies have no completion path. A merge between
them is rejected because the policies differ, and `release-capability-governance` works only on
a **draft** policy. Amending an approved policy is done by forking it, but a fork carries the
whole governed set, so it cannot drop one Capability. Say so plainly and do not invent a
workaround: do not edit the graph another way, do not retry, and do not use the
acknowledgment, which does not apply. A follow-on change to let a fork draft drop a single
Capability is tracked separately; this skill must not pretend it exists.

## Reconciliation of an interrupted approval

The passkey is signed in a browser, so an approval can be signed and then be interrupted before
its outcome is recorded. `check-cleanup-approval` settles such an approval lazily. When a signed
approval has no outcome and is more than five minutes past its expiry, the tool checks the
graph for the edit:

- the edit is present: the outcome becomes `reconciled: applied`, and the officer is told the
  change was found in the graph after an interrupted run;
- the edit is absent: a `failed` audit row with reason `interrupted_no_effect` is recorded
  under the same approval id and the outcome becomes an `error`; nothing was changed.

An `applied` audit row followed by a `failed` row for one approval id means no edit occurred.
An approval still inside that window is reported as it is (`pending`, `expired`, or `signed`
without an outcome): say so and let the officer retry the check later; never poll in a loop.
Only the officer who created the approval can see it.

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
