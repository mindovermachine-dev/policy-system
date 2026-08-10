<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System — Cross-Source Dataset Alignment Strategy

**Status:** Draft

---

## Purpose

Capture the architectural decision for combining independently produced datasets
(such as Engineering Practices seed data and EU regulation extracts) without
creating a disjoint graph that breaks cross-source reasoning.

## Context

The repository currently contains:

- External regulation datasets (CRA, NIS2, GDPR) loaded through ingestion spikes.
- Internal engineering-practices seed data generated as a standalone dataset.

The question is whether datasets can be mixed and matched over time, or whether
we must build one fully aligned dataset up front (potentially one per industry).

## Problem Statement

Independent dataset creation without an explicit alignment contract tends to
produce semantic duplicates and orphaned traversals:

- Same concept represented as different Capability or Obligation nodes.
- Cross-source queries that appear complete but silently miss relevant chains.
- Deferred reconciliation work that becomes harder and more expensive later.

## Decision Drivers

- DR-001: Preserve trustworthy cross-source query behavior.
- DR-002: Keep ingestion incremental (no forced full rebuild for each source).
- DR-003: Keep provenance intact per source dataset.
- DR-004: Make disjoints visible and governable, not hidden.
- DR-005: Support industry-specific variation without fragmenting core semantics.

## Architectural Options

### Option A: Free mix-and-match with no alignment gates

- Fastest ingestion onboarding.
- Highest disjoint risk and growing hidden query inaccuracy.
- Reconciliation becomes reactive and expensive.

### Option B: Single fully aligned monolithic dataset per domain or industry

- Strong consistency inside each curated dataset.
- Slower to evolve and expensive to maintain as sources change.
- Duplicates alignment effort across industries and increases drift between
  monoliths.

### Option C: Canonical core plus source packages with mandatory alignment gates

- One shared semantic core (especially Obligation and Capability semantics).
- Each source dataset retains provenance and local identifiers.
- Alignment is enforced at load time through mapping quality gates.
- Industry-specific behavior is modeled as overlays, not separate semantic cores.

## Decision

Choose **Option C**.

The Policy System will support cross-source reasoning by using a canonical core
with governed source-package alignment, rather than relying on ungoverned
mix-and-match or fully separate monoliths.

## Decision Details

### AD-ALIGN-001: Canonical semantic spine is shared

Across sources, the semantic spine remains shared:

- `Obligation` and `Capability` are canonical convergence points.
- `Regulation`, `Role`, and `Requirement` remain source-scoped by design.
- `Policy`, `Standard`, and `Control` may be source-specific but must connect to
  canonical `Capability` nodes for cross-source reasoning.

### AD-ALIGN-002: Every source load must declare alignment state

Each inbound source entity participating in convergence must resolve to one of:

- `exact_match`
- `mapped_with_review`
- `unmapped`

`unmapped` is allowed only as an explicit, tracked exception.

### AD-ALIGN-003: Alignment quality gates are required at ingest

A source package may be loaded, but cross-source query trust must be downgraded
unless alignment gates pass.

Minimum gate set:

- AG-001: Capability mapping coverage >= defined threshold.
- AG-002: Obligation mapping coverage >= defined threshold.
- AG-003: Ambiguous mappings above confidence threshold require review.
- AG-004: Unmapped critical entities must create explicit remediation entries.

### AD-ALIGN-004: Industry-specific modeling uses overlays

Industry specialization should be added as overlays (additional
Role/Requirement/Policy/Standard/Control content) mapped into the same canonical
Obligation/Capability core.

Do not create separate canonical cores per industry unless the domain model
itself is revised.

## Consequences

### Positive

- Preserves cross-source reasoning quality while keeping incremental ingestion.
- Makes graph disjoints explicit and measurable.
- Avoids duplicating semantic cores per industry.

### Negative

- Introduces alignment governance work and gating logic.
- Requires curation workflows for ambiguous or unmapped entities.

### Cost of Not Doing This

- Increasing hidden disjoints over time.
- False confidence in cross-source query completeness.
- Higher eventual migration and remediation cost.

## Non-Goals

- This decision does not define specific threshold values for AG-001..AG-004.
- This decision does not redesign the domain model labels/edges.
- This decision does not replace existing strict record-level quality rubrics;
  it adds cross-source semantic alignment governance on top.

## Adoption Plan (Prototype)

1. Add source-package metadata for alignment status per converging entity.
2. Add ingest report fields for AG-001..AG-004 outcomes.
3. Surface cross-source trust indicators in query responses when gates fail.
4. Track and burn down `unmapped` entities before claiming full cross-source
   coverage.
5. Reassess thresholds after two additional source-package onboarding cycles.

## Related Documents

- [docs/architecture/ps-prototype-architecture.md](docs/architecture/ps-prototype-architecture.md)
- [docs/artifacts/ps-domain-concepts.md](docs/artifacts/ps-domain-concepts.md)
- [docs/test-data/engineering-practices/minimal-engineering-policy-seed-guideline.md](docs/test-data/engineering-practices/minimal-engineering-policy-seed-guideline.md)
- [docs/test-data/rubrics/policy-standard-control-strict-rubrics.md](docs/test-data/rubrics/policy-standard-control-strict-rubrics.md)

---

*End of Document*
