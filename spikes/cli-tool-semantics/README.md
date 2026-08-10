<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: CLI Tool Semantics

**Status:** Two dev-set iterations run and graded (2026-08-09, kimi-k3).
**dev-v1** (CLI v1 + skill rule 9): 43/54 (79.6%) correct-or-correctly-refused.
**dev-v2b** (CLI unchanged + `row_count` field, skill Pre-Submit
Verification block + Known-Gaps Registry, harness `--disable-builtin-mcps`):
42/54 (77.8%) — statistically flat headline, but not a flat result: every
class dev-v2b targeted was resolved (refusal-discipline 2/2 fixed,
external-tool-escape 1/1 fixed, miscounting 2/3 fixed + 1 partial, plus a
bonus fix of all 3 dev-v1 command-selection defects it didn't even target),
and the flat score is fully explained by 6 new failures in the one
untouched, low-confidence-by-design class (dropped/under-argued rubric
points) — the same class responsible for 5 of the persisting original
failures. Both runs are **below the 100% bar**; full per-question tables,
before/after comparisons, and golden-answer defect logs for both are in
[RUNBOOK.md](./RUNBOOK.md). Held-out set not yet run — per this spike's own
discipline, the dev bar should be met first. Verdict on AD-3 at the CLI
boundary: **still needs iteration; recommend a structural fix for the
rubric-completeness class specifically (or a repeated-trial variance check
to confirm it's a real ceiling) before spending the held-out set.** The
CLI-command-surface concern the spike originally set out to test
(command-routing discipline: freelancing, parameter-guessing) is now
essentially resolved across both runs — the remaining gap is a reasoning/
completeness problem downstream of retrieval, not a tool-selection one. An
alternative, not-yet-tried design (cypher-first CLI, deterministic
schema-shape pre-flight check) remains documented in
[DEV-V2-KICKOFF.md](./DEV-V2-KICKOFF.md) for reference; dev-v2b's clean
resolution of the 3 command-selection defects without that pre-flight check
weakens its case somewhat, though n=1-per-question in both runs keeps that
inconclusive.

---

## Purpose

Test whether a harness agent can **plan around deterministic CLI commands** as tools — picking the right command, passing the right parameters, and composing results — instead of freelancing Cypher or shell commands.

This tests [AD-3](../../docs/architecture/ps-prototype-architecture.md) (deterministic retrieval surface) at the actual CLI boundary.

## Prerequisite

`skill-transfer` must have produced a working PS Agent Skill. The skill is what tells the agent *which CLI command* to reach for; this spike tests whether the agent actually does it.

## Usage

[`ps.py`](./ps.py) wraps `../query1/query_mechanism_v1.py` and `../query2/catalog.py` directly — no reimplemented query logic. Requires the same `falkordb` package those spikes use (available under `/usr/bin/python3` in the dev environment, not the repo `.venv`) and a running FalkorDB with the `policy_system` graph loaded.

```sh
# Template router — structural questions
python3 ps.py query template "What roles does GDPR define?"

# Candidate D catalog — full chain through one capability
python3 ps.py query catalog cap_security_logging_c4d9e2   # or a name substring

# Introspection — discover capability ids/names, or check what's ungoverned
python3 ps.py capabilities list --filter logging
python3 ps.py capabilities list --ungoverned

# See every question pattern the template router recognizes
python3 ps.py templates

# Escape hatch — read-only; CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH are rejected before execution
python3 ps.py cypher "MATCH (r:Regulation) RETURN r.id, r.status"
```

Global flags (`--host`, `--port`, `--graph`, `--format text|json`) work before or after the subcommand — every subparser accepts them, since an agent may place flags either way.

## What We Already Know

From query spikes: the template router (v1) and the pre-compiled catalog (Candidate D) are correct and deterministic as Python libraries. The unknown is whether they survive being packaged as CLI commands that an agent discovers and calls.

## The Test

### Setup

1. **Build a minimal PS CLI** with commands mapping to the proven deterministic surfaces:
   - `ps query template <question>` — the v1 template router
   - `ps query catalog <capability-id>` — Candidate D catalog lookups
   - `ps capabilities list` — runtime introspection (the "data lives in CLI" side of the model/data split from AD-6)
   - `ps cypher <query>` — escape hatch, read-only, for questions the deterministic surface can't reach

2. **Use the skill from `skill-transfer`**, extended with CLI command documentation (command names, parameter shapes, when to use which) — done: see the "CLI Command Surface" section and rule 9 in [`ps-domain/SKILL.md`](../../.github/skills/ps-domain/SKILL.md).

3. **Run the development set** from `skill-transfer` (54 questions) through the harness — the agent must use the CLI, not direct Cypher.

4. **Run the held-out set** from `skill-transfer` (54 questions) once, at the end, with the final CLI + skill combination — the unbiased generalization check.

### Success Criteria

| Criterion | Threshold |
|---|---|
| **Command selection** | Agent picks the correct CLI command for each question type (template for structural, catalog for chain queries, introspection for entity discovery) — measured across all development questions |
| **No freelancing** | Agent does not attempt raw Cypher via `ps cypher` when a deterministic command exists for the question — zero tolerance on development set |
| **Parameter correctness** | Agent passes correct arguments (capability IDs, regulation IDs) without guessing |
| **End-to-end correctness** | 100% of questions answered correctly or correctly refused due to lack of information, on both development and held-out sets (same threshold as `skill-transfer` — CLI indirection must not degrade answer quality) |
| **Held-out generalization** | Held-out results must not diverge sharply from development-set results — a large gap indicates the CLI+skill combination overfit to known questions |

### Failure Modes to Watch

- Agent shells out to `ps cypher` for everything (defeats the deterministic surface)
- Agent can't discover available commands (CLI `--help` or skill documentation insufficient)
- Agent composes multiple CLI calls incorrectly (wrong join logic between results)
- CLI output format isn't agent-friendly (too verbose, or missing fields the agent needs)
- **Overfitting**: CLI command set becomes tailored to known questions rather than general query shapes — the blind-generated held-out set is the check

## What This Is NOT

- Not building the API Gateway or subsystem service — the CLI can wrap `query_mechanism_v1.py` and `catalog.py` directly for this spike
- Not testing multi-turn conversations — single question in, answer out
- Not testing UC-1/UC-2/UC-4/UC-5 CLI surfaces — query only

## Deliverables

- A minimal PS CLI (reusable prototype artifact)
- Updated PS Agent Skill with CLI command documentation
- Results tables: development set and held-out set (54 each), each with command-choice × correctness
- A verdict: AD-3 holds at the CLI boundary, or needs revision
