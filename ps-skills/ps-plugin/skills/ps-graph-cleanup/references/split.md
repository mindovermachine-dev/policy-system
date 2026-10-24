# Splitting an automatic resolution (ps-graph-cleanup)

Read on demand from `SKILL.md`. Every Guardrail in `SKILL.md` still applies.

When Company Merge resolves an incoming Capability X onto an existing Capability A by
similarity, X is never created and a `ResolvedCapability` marker records the resolution, so every
later ingest, amendment or restore follows it. `split-capability` undoes one such resolution
after a preview, an acknowledgment when A is governed, and a passkey approval. It takes `x_id`,
the id from `list-capability-resolutions`, and optionally `acknowledge_governance_change`.

## What the preview shows

The preview lists, per edge type (`requires`, `covers`, `mitigated_by`), the ids of the edge
sources in four buckets, decided from the regulation baselines that hold X or A:

| Bucket   | Meaning                                                                                 |
| -------- | --------------------------------------------------------------------------------------- |
| `moves`  | Some baseline holds the edge with X and none with A: it leaves A and is put on X        |
| `shared` | Some baseline holds the edge with X and some with A: it stays on A and is also put on X |
| `added`  | Some baseline holds the edge with X and the source has no edge on A today: X only       |
| `stays`  | Every other edge of A: unchanged                                                        |

It also shows `obligations_moving` (the Obligations whose `REQUIRES` edge moves to X),
`source_baseline` (the baseline X's name and embedding come from), `embedding_available`,
`skipped_missing_endpoints`, and `governance`: `null` when A is ungoverned, otherwise A's policy
with `acknowledgment_required` and `acknowledgment_text`.

## What the split does

On signing, one all-or-nothing graph write, audited first as `capability.split`:

- X is created as its own Capability with the name and embedding from that baseline, and starts
  with no governing policy, so the gap it exposes is visible to a gap question.
- The edges move as previewed; A keeps its properties and its `GOVERNED_BY` edge.
- The `ResolvedCapability` marker for X is removed. Later ingests, amendments and restores that
  regenerate X match it exactly and never resolve it onto A again.

A split never edits a Policy, a Standard or a Control. When A is governed, its policy's governed
set does not change, but the Obligations in `obligations_moving` are no longer covered by it;
that is the governance change the officer acknowledges. The acknowledgment is part of what the
passkey signs and of the audit row.

A wrong split is reversed with `merge-capabilities`, absorbing X into A; no separate undo
exists. That merge follows its own flow in `SKILL.md`, including its own acknowledgment when A
is governed.

## Named states

| Tool result                                                   | Named state to report                                                                  |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| the gate and graph errors listed in the discovery table       | the same named states as discovery                                                     |
| an `error:` saying there is no automatic resolution of the id | "No automatic resolution": check the id against `list-capability-resolutions`          |
| an `error:` saying the id already exists as a Capability      | "Already a Capability": there is nothing to split                                      |
| an `error:` saying the id was merged by a cleanup merge       | "Cleanup merge": a cleanup merge is reversed with `unmerge-entities`, not split        |
| an `error:` saying no regulation baseline holds the id        | "No baseline": X cannot be recreated; report it, never invent its name                 |
| `acknowledgment_required: true` (no approval created)         | "Acknowledgment needed": show the `message`, get an explicit yes, then repeat the call |
| `split: true` in the `check-cleanup-approval` outcome         | Done: X is its own Capability and the resolution is gone                               |
| `outcome.error` of a signed approval                          | Report the message; the approval is spent, so a retry needs a new preview and approval |

For every other error and outcome, read `references/error-reference.md`.
