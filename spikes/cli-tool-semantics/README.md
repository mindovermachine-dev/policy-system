<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: CLI Tool Semantics

**Status:** Planned (depends on `skill-transfer`)

---

## Purpose

Test whether a harness agent can **plan around deterministic CLI commands** as tools — picking the right command, passing the right parameters, and composing results — instead of freelancing Cypher or shell commands.

This tests [AD-3](../../docs/architecture/ps-prototype-architecture.md) (deterministic retrieval surface) at the actual CLI boundary.

## Prerequisite

`skill-transfer` must have produced a working PS Agent Skill. The skill is what tells the agent *which CLI command* to reach for; this spike tests whether the agent actually does it.

## What We Already Know

From query spikes: the template router (v1) and the pre-compiled catalog (Candidate D) are correct and deterministic as Python libraries. The unknown is whether they survive being packaged as CLI commands that an agent discovers and calls.

## The Test

### Setup

1. **Build a minimal PS CLI** with commands mapping to the proven deterministic surfaces:
   - `ps query template <question>` — the v1 template router
   - `ps query catalog <capability-id>` — Candidate D catalog lookups
   - `ps capabilities list` — runtime introspection (the "data lives in CLI" side of the model/data split from AD-6)
   - `ps cypher <query>` — escape hatch, read-only, for questions the deterministic surface can't reach

2. **Use the skill from `skill-transfer`**, extended with CLI command documentation (command names, parameter shapes, when to use which).

3. **Run the development set** from `skill-transfer` (~20 questions) through the harness — the agent must use the CLI, not direct Cypher.

4. **Run the held-out set** from `skill-transfer` (~20 questions) once, at the end, with the final CLI + skill combination — the unbiased generalization check.

### Success Criteria

| Criterion | Threshold |
|---|---|
| **Command selection** | Agent picks the correct CLI command for each question type (template for structural, catalog for chain queries, introspection for entity discovery) — measured across all development questions |
| **No freelancing** | Agent does not attempt raw Cypher via `ps cypher` when a deterministic command exists for the question — zero tolerance on development set |
| **Parameter correctness** | Agent passes correct arguments (capability IDs, regulation IDs) without guessing |
| **End-to-end correctness** | 50% on development set (same threshold as `skill-transfer` — CLI indirection must not degrade answer quality); 50% on held-out set |
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
- Results tables: development set and held-out set (~20 each), each with command-choice × correctness
- A verdict: AD-3 holds at the CLI boundary, or needs revision
