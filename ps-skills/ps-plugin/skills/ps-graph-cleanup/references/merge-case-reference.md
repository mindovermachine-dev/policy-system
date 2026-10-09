# Merge case reference (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

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
