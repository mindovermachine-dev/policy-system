# Known limitation (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

## Known limitation

Two Capabilities governed by two approved policies have no completion path. A merge between
them is rejected because the policies differ, and `release-capability-governance` works only on
a **draft** policy. Amending an approved policy is done by forking it, but a fork carries the
whole governed set, so it cannot drop one Capability. Say so plainly and do not invent a
workaround: do not edit the graph another way, do not retry, and do not use the
acknowledgment, which does not apply. A follow-on change to let a fork draft drop a single
Capability is tracked separately; this skill must not pretend it exists.
