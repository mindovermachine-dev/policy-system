# Transition and governance output shapes (ps-policy-lifecycle)

Read on demand from `SKILL.md`'s Process or Output after a successful `propose-policy`/`approve-policy`/`reject-policy`/`revert-policy-to-draft`, or when `create-policy-draft` returns a non-empty `governed_capability_ids`. Every Guardrail in `SKILL.md` still applies.

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

and, when `governed_capability_ids` is non-empty (an approved fork took
over its prior's Capabilities):

```text
  Capabilities now governed by this Policy: <governed_capability_ids>
```

`create-policy-draft` reports `governed_capability_ids` (the Capabilities a
fresh draft claimed; empty for a fork or when none were passed) in its
created-Policy confirmation when non-empty:

```text
  Governs Capabilities: <governed_capability_ids>
```
