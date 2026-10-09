# Transition rules (ps-policy-lifecycle)

<!-- markdownlint-disable MD029 -- list numbers keep the step numbers they had in SKILL.md's Process -->

Read on demand from `SKILL.md`'s Process or Output before calling `propose-policy`, `approve-policy`, `reject-policy` or `revert-policy-to-draft`. Every Guardrail in `SKILL.md` still applies.

3. **Know who can do what, and from which status, before calling:**

   | Action                   | Who                                                                                                                                       | Required current status | Resulting status                                                                                                                                                                                                                                                                                                                                                           |
   | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `create-policy-draft`    | any authenticated caller (becomes the owner; no role is required to claim Capabilities via `capability_ids`)                              | — (new Policy)          | `draft`                                                                                                                                                                                                                                                                                                                                                                    |
   | `get-policy`             | Draft: owner, or a caller holding `SystemOwner`/`SystemAdmin`. Proposed/Approved/Deprecated: any authenticated caller.                    | any                     | unchanged (read-only)                                                                                                                                                                                                                                                                                                                                                      |
   | `propose-policy`         | owner only (no role required)                                                                                                             | `draft`                 | `proposed`                                                                                                                                                                                                                                                                                                                                                                 |
   | `approve-policy`         | a caller holding `PolicyManager`, and never the Policy's own owner (self-approval always blocked, even for a `PolicyManager` who owns it) | `proposed`              | `approved` (and, if this Policy has an approved prior linked via `SUPERSEDED_BY`, that prior's whole tree auto-cascades to `deprecated` in the same call, as its own separate event; for a fork, the Capabilities the prior governed move to this Policy in the same operation as the status change, and a fork whose prior governs none approves without moving any edge) |
   | `reject-policy`          | same as `approve-policy` (`PolicyManager`, never the owner)                                                                               | `proposed`              | `draft`                                                                                                                                                                                                                                                                                                                                                                    |
   | `revert-policy-to-draft` | owner only — **not** role-gated; a `PolicyManager` who isn't the owner is rejected exactly like any other non-owner                       | `proposed`              | `draft`                                                                                                                                                                                                                                                                                                                                                                    |

   Every action's cascade applies to the Policy **and every Standard/Control
   in its tree** in one write. `approve-policy`/`reject-policy` require the
   caller and owner to differ in _either_ subject or issuer — the same
   subject under a different issuer is treated as a different person and
   may approve/reject (this also means it is never blocked as
   self-approval).

4. **Completeness gate (propose only):** `propose-policy` requires the
   Policy to have at least one `Standard` attached; a Standard with zero
   Controls is never checked. There is no minimum for approve/reject/revert
   beyond the status/ownership/role gates above.
