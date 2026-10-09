# Unmerging a merge (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

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
