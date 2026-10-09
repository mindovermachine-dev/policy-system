# Reconciliation of an interrupted approval (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

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
