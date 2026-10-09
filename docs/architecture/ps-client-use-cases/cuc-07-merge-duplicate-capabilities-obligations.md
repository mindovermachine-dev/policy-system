<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-07: Merge duplicate Capabilities/Obligations

**Status:** Specified
**Actor:** Compliance Officer — an explicit, exact-match `ComplianceOfficer` grant;
SystemOwner/SystemAdmin get no override, and the gate fails closed
**Goal:** Pipeline-made duplicates cleaned up by human judgement: near-duplicate
Capabilities and same-Role duplicate Obligations found, previewed, and merged — or
released, or unmerged — each write behind its own passkey approval.
**Realizes:** —
**Interaction class:** Review/resolve
**v1:** yes

This use case shares CUC-06's comparison-artifact and danger-zone shape; only the
differences are spelled out here. The structural difference: CUC-06 resolves reviews the
system already minted, while here the user initiates discovery and chooses the pair *and
the direction*.

## Entry points

- A new conversation: "find duplicate capabilities," typically post-ingest hygiene after
  CUC-04/CUC-05 runs.
- The Library (CUC-11): resuming a cleanup session, mid-approval included.

## Preconditions

- Caller is authenticated with the explicit `ComplianceOfficer` grant — the one named
  state with no workaround is "grant missing: ask a SystemOwner/SystemAdmin to grant it."
- Duplicates exist to find; discovery reporting "no candidate groups" is a valid end.

## Beats

1. **Discover.** Either sweep is read-only and renders as a report: Capability candidate
   groups (each member's name, obligation count, governing policy; the group's evidence
   `basis` and its `merge_case`) or duplicate Obligations grouped under one Role only —
   identical wording under two Roles is two duties, never a candidate — with each
   member's `source_ref`s so the officer can check the regulation really states the duty
   twice. Every group and field reported, none invented, none dropped.
2. **Judge.** A candidate group is a suggestion, never a verdict. The officer picks the
   pair and the direction: which node **survives**, which is **absorbed**. The client
   never chooses; chat never recommends on group evidence alone. The comparison artifact
   states the kind-specific fate plainly — an absorbed Capability remains as a `merged`
   tombstone pointing at the survivor; an absorbed Obligation is **deleted** (its
   snapshot held in the audit row, a marker left so re-ingestion attaches to the
   survivor instead of recreating it).
3. **Preview.** The preview call never edits the graph. The comparison artifact renders
   its full effect: edges to move per class, duplicates collapsed, obligations affected,
   and the governance consequence.
4. **Acknowledge governance (Capability case 2 only).** When exactly one side is
   governed, no approval exists yet: the artifact shows the governance block — the
   policy, the coverage change, governed set before and after — and the officer must
   acknowledge the governance change *in their own words* before the preview is re-run
   with the acknowledgment set. The acknowledgment becomes part of what the passkey
   signs and of the audit row. Case 3 with two *different* policies is blocked outright:
   the artifact relays the named error and the one legal path — release the absorbed
   Capability from its policy first, possible only while that policy is a draft;
   otherwise there is no completion path, and the client says so rather than inventing
   one.
5. **Confirm, sign, resolve.** As CUC-06's merge path: the officer confirms in their own
   words that the two are one duty and the survivor is right; the passkey ceremony signs
   an approval bound to this exact pair and previewed state, valid 15 minutes; the
   outcome is read back (`merged`, a spent approval's `error` — graph untouched, start
   over from preview — or `reconciled: applied` after an interrupted run). Expired means
   re-preview, never re-sign.
6. **Reverse.** `unmerge` restores a merged pair from its audit snapshot through the same
   preview → confirm → passkey shape — cleanup decisions are reversible by ceremony,
   unlike CUC-06's near-miss merge.

## Artifacts

- Discovery reports (read-only), one per sweep.
- One **comparison artifact** per pair taken up: fate statement, effect preview,
  governance block when applicable, decision controls, resolved record.

## Approvals & notifications

- **One passkey ceremony per write** — merge, release, unmerge alike — against PS
  Service's own RP, each bound to its exact pair and previewed state. Discovery never
  creates an approval.
- The case-2 governance acknowledgment is an additional, distinct acknowledgment *inside*
  the flow, captured before any approval exists.
- No Inbox items or notifications today; discovery is on-demand.

## Contracts touched

- **H-1 Tool loop** — discovery, preview, approval-status reads as client tool calls;
  pending approvals resumable across conversations.
- **H-4 Permissions & approvals** — this flow is the contract's stress test: effect
  preview, free-text acknowledgment, and confirm-in-own-words layered before the
  ceremony.
- **PSC-6 Conversation persistence** — cleanup sessions resume with their artifacts and
  any pending approval.
- **PSC-9 Auth flows** — the `approval_url` companion-browser ceremony is the SP-4 gap,
  here with first-time passkey enrollment on the signing page as an added case.
- **PSC-10 Artifact type registry** — report and comparison flags drive the pane.

## Open points

- Whether periodic duplicate sweeps should mint system-generated Inbox items (parity
  with CUC-06's near-misses) or stay on-demand — if they do, Q-12 governs them too.
- The "in their own words" acknowledgments: H-4 covers previews and acknowledgments, but
  free-text capture that is then bound into a signed approval is a control shape the
  Artifact Pane design has not pinned down.
- Survivor/absorbed direction choice on the comparison artifact — CUC-06's pairs arrive
  ordered by the review; here direction is the officer's input, and the kind-specific
  fate (tombstone vs delete) must be impossible to miss at the moment of choice.
