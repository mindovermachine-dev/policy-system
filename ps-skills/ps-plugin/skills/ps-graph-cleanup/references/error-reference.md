# Error reference (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

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
