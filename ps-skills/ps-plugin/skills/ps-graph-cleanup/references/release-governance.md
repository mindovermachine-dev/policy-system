# Releasing a capability from a policy (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

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
