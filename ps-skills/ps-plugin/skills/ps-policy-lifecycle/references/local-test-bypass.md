# Local-test-bypass caveat (ps-policy-lifecycle)

<!-- markdownlint-disable MD029 -- list numbers keep the step numbers they had in SKILL.md's Process -->

Read on demand from `SKILL.md`'s Process or Output when the PS Service connector is running under the local-test bypass. Every Guardrail in `SKILL.md` still applies.

5. **Local-test-bypass caveat:** under the local-test bypass, every one of
   these six tools treats the bypass as a real authenticated caller, always
   resolving to the same fixed identity for both the acting caller and (for
   Policies it creates) the owner. Because `approve-policy`/`reject-policy`
   always compare the same fixed identity against itself, self-approval is
   always blocked under the bypass — **`approve-policy` and `reject-policy`
   are not exercisable under the local-test bypass**; only
   `create-policy-draft`/`get-policy`/`propose-policy`/
   `revert-policy-to-draft` can be meaningfully tested that way.
