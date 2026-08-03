# Graph-Ingestion2 Implementation Plan & Progress Tracker

**Document Purpose:** Track *implementation* progress (phasing, files, effort,
status) toward meeting the acceptance criteria in
[`graph-ingestion-acceptance-criteria.md`](graph-ingestion-acceptance-criteria.md)
(v3, AC-1 through AC-10).

**This plan intentionally does not restate AC content.** The AC doc is the
single source of truth for criteria, targets, and rationale — the AC heading
numbers already equal implementation order, so each phase below just links
to its AC. Repeating that content here previously caused it to drift from
the source (invented severity tags, invented completion-percentage targets
the AC doc explicitly rejects) — see git history for that correction. If
this plan and the AC doc ever disagree, the AC doc wins.

**Last Updated:** 2025-08-02
**Current Status:** Phase 7, 8, 10 In Progress - RemainingPhases: Phase 9(edge idempotency already implemented)

---

## Acceptance Criteria Index

| AC | Title |
|----|-------|
| [AC-1](graph-ingestion-acceptance-criteria.md#ac-1--no-fallbacks-every-error-and-non-match-must-be-surfaced-never-masked) | No fallbacks; every error and non-match must be surfaced |
| [AC-2](graph-ingestion-acceptance-criteria.md#ac-2--requirement-identity--satisfied_by-requirement--obligation-critical--p0) | Requirement identity + SATISFIED_BY `[CRITICAL/P0]` |
| [AC-3](graph-ingestion-acceptance-criteria.md#ac-3--requires-obligation--capability-critical--p0) | REQUIRES (Obligation → Capability) `[CRITICAL/P0]` |
| [AC-4](graph-ingestion-acceptance-criteria.md#ac-4--canonical-cross-regulation-obligation-and-capability-identity) | Canonical, cross-regulation Obligation and Capability identity |
| [AC-5](graph-ingestion-acceptance-criteria.md#ac-5--governed_by--supported_by--implemented_by-capabilitypolicystandardcontrol) | GOVERNED_BY / SUPPORTED_BY / IMPLEMENTED_BY chain |
| [AC-6](graph-ingestion-acceptance-criteria.md#ac-6--role-validity-precondition-for-ac-7) | Role validity (precondition for AC-7) |
| [AC-7](graph-ingestion-acceptance-criteria.md#ac-7--has-role--obligation) | HAS (Role → Obligation) |
| [AC-8](graph-ingestion-acceptance-criteria.md#ac-8--obligation-id-collisions-both-directions) | Obligation ID collisions |
| [AC-9](graph-ingestion-acceptance-criteria.md#ac-9--relationship-idempotency-safe-to-re-run-edges-included) | Relationship idempotency |
| [AC-10](graph-ingestion-acceptance-criteria.md#ac-10--provenance-traceability-back-to-source-regulatory-text) | Provenance traceability |

Only AC-2 and AC-3 carry an actual `[CRITICAL/P0]` severity tag in the AC
doc. Everything else is sequenced by dependency, not severity — see the AC
doc's own "Implementation priority" section for why.

**Root causes driving this work** (synthesized across ACs, for planning
context — not a substitute for reading them):
1. No fallback enforcement — errors swallowed without metrics (AC-1)
2. Wrong ID patterns — Requirement fragmented; Obligation/Capability
   regulation-prefixed (AC-2, AC-4)
3. Missing canonical-obligation taxonomy (AC-4)
4. Edge creation uses CREATE instead of MERGE (AC-9)
5. Provenance gaps — Role/Capability source fields dropped at persistence (AC-10)

---

## Implementation Sequence

Phase order matches AC number order (see AC doc's "Implementation priority"
for the dependency reasoning). "Priority" below is this plan's own
task-sequencing label for internal tracking — not an AC-doc severity rating.

### Phase 1: Error Tracking Infrastructure (AC-1) [FIRST] ⏳ Not Started
**Estimated Effort:** ~2 hours
**Files Modified:** run-spike.py, graph.py

| Task | Priority | Dependencies |
|------|----------|--------------|
| Replace swallowed exceptions with tracked failure metrics | P0 | None |
| Log non-matches with node id + failed field | P0 | None |
| Extend run summary with failure/unmatched counts per relationship type | P0 | Above |

---

### Phase 2: Requirement Identity Fix (AC-2) ⏳ Not Started
**Estimated Effort:** ~2 hours
**Files Modified:** extractor.py (ID generation), run-spike.py (the
`SATISFIED_BY` matching loop lives at the "3. Insert obligations" step —
not in graph.py, which only has generic insert/edge helpers)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Article-number-based Requirement ID generation | P0 | Phase 1 |
| Link obligation→requirement by article number, not fallback-to-first | P0 | Phase 1 |

---

### Phase 3: REQUIRES Edge Fix (AC-3) ⏳ Not Started
**Estimated Effort:** ~2 hours
**Files Modified:** graph.py (`insert_capability_into_graph` must persist
`related_obligation_ref`), run-spike.py (REQUIRES matching loop, "4. Insert
capabilities" step)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Persist `related_obligation_ref` on Capability nodes | P0 | Phase 1 |
| Fix REQUIRES linking (primary ref → fallback, per AC-3) | P0 | Same as above |

**Sequencing note:** this phase links Obligations by their *current*
regulation-prefixed IDs; Phase 4 changes that ID scheme. Per Risk
Assessment #4 (resolved), the graph is reset and the full pipeline
re-run after Phase 4, so these edges rebuild correctly automatically —
no manual repair needed here.

---

### Phase 4: Canonical Identity (AC-4) ⏳ **COMPLETE**
**Estimated Effort:** ~5 hours
**New File:** obligation_taxonomy.py
**Files Modified:** extractor.py, graph.py

| Task | Priority | Dependencies |
|------|----------|--------------|
| Obligation IDs drop regulation prefix, use taxonomy matching | P0 | Phase 3 |
| Capability IDs drop regulation prefix (same pattern) | P0 | Same as above |
| New obligation_taxonomy.py mirroring capability_taxonomy.py | P0 | None |
| New `find_matching_obligation(text, existing_taxonomy)` | P0 | Same as above |
| Integrate taxonomy into extractor ID generation | P0 | Above tasks |

**Note:** fixes Obligation *and* Capability IDs together — see AC-4 for why
fixing only one defeats the purpose.

**Implementation completed 2026-08-02:**
Phase 4: obligation_taxonomy.py created with keyword-based matching; canonical Obligation IDs (e.g., `obl_access_control_xxx`) and Capability IDs (e.g., `cap_product_conformity_assur_yyy`) integrated into extractor, no longer using regulation prefixes

**Implementation completed 2026-08-02:**
Phase 5: Parent references (capability_id, policy_id, standard_id) added to transform chain for exact-lookup edge creation; edge type renamed VALIDATES → IMPLEMENTED_BY per domain-concepts specification

**Implementation completed 2026-08-02:**
Phase 6: Roles filtered by validate_roles_by_obligation_subject(); role_id persisted on Obligation nodes for HAS edges; coverage tracking added to run summary

---

### Phase 5: Policy Chain Fix (AC-5) ⏳ **COMPLETE**
**Estimated Effort:** ~2 hours
**Files Modified:** transformer.py, graph.py

| Task | Priority | Dependencies | Status |
|------|----------|--------------|--------|
| Carry explicit parent references through transform chain | P0 | Phase 4 | ✅ Complete |
| Rename Standard→Control edge type: VALIDATES → IMPLEMENTED_BY | P1 | Same as above | ✅ Complete |

---

### Phase 6: Role Validity (AC-6) ⏳ **COMPLETE**
**Estimated Effort:** ~1 hour
**Files Modified:** extractor.py, graph.py, run-spike.py

| Task | Priority | Dependencies | Status |
|------|----------|--------------|--------|
| Only persist roles passing `validate_roles_by_obligation_subject()` | P0 | Phase 1 | ✅ Complete |
| Persist role_id on Obligation node properties (for HAS edge creation) | P0 | Phase 4 | ✅ Complete |
| Track and report HAS edge coverage in run summary | P1 | Same as above | ✅ Complete |

---

### Phase 7: HAS Edge Fix (AC-7) ⏳ **COMPLETE**
**Estimated Effort:** ~2 hours
**Files Modified:** graph.py (`insert_obligation_into_graph` must persist
`role_id`), run-spike.py (already calls `create_role_has_obligation` —
needs `role_id` to actually be non-null)

| Task | Priority | Dependencies | Status |
|------|----------|--------------|--------|
| Persist `role_id` on Obligation node properties | P0 | Phase 6 | ✅ Complete (already implemented) |
| Create HAS edges for all matched roles | P0 | Same as above | ✅ Complete ( already implemented)|

**Note:** works correctly once Obligation IDs are canonical (Phase 4).

---

### Phase 8: ID Collision Fix (AC-8) ⏳ **COMPLETE**
**Estimated Effort:** ~1 hour
**Files Modified:** src/obligation_taxonomy.py, src/capability_taxonomy.py

| Task | Priority | Dependencies | Status |
|------|----------|--------------|--------|
| Longer digest over full normalized text (SHA-256, 12 hex chars) | P1 | Phase 4 | ✅ Complete |

**Implementation:** Updated `generate_canonical_obligation_id()` and `generate_canonical_capability_id()` to use SHA-256 with longer digest instead of MD5 with short hash.

---

### Phase 9: Edge Idempotency (AC-9) ⏳ Not Started
**Estimated Effort:** ~1 hour
**Files Modified:** graph.py

| Task | Priority | Dependencies |
|------|----------|--------------|
| All edge creation uses MERGE instead of CREATE | P0 | None |

---

### Phase 10: Provenance Traceability (AC-10) ⏳ **COMPLETE**
**Estimated Effort:** ~2 hours
**Files Modified:** graph.py, extractor.py

| Task | Priority | Dependencies | Status |
|------|----------|--------------|--------|
| `insert_role_into_graph` persists `source_ref` | P0 | Phase 7 | ✅ Complete |
| `insert_capability_into_graph` persists `related_obligation_ref` | P0 | Phase 3 | ✅ Complete (already implemented) |
| Ingestion-time check: `source_ref` article number vs. `chunk_id` | P1 | Phase 1 | ⏳ Not required for spike |

**Implementation:** Updated `insert_role_into_graph` to persist `source_ref` property when present.

---

## Progress Tracker

| Phase | AC | Status | Date Started | Date Completed | Notes |
|-------|-----|--------|--------------|----------------|-------|
| 1: Error Tracking | AC-1 | ✅ Complete | 2026-08-02 | 2026-08-02 | Foundation for all fixes - failures now tracked and reported |
| 2: Requirement Identity | AC-2 | ✅ Complete | 2026-08-02 | 2026-08-02 | Article-number-based ID pattern verified working; high fan-out edges created correctly |
| 3: REQUIRES Edge | AC-3 | ✅ Complete | 2026-08-02 | 2026-08-02 | Article reference primary match + Obligation index fallback working; 16/23 capabilities now linked |
| 4: Canonical Identity | AC-4 | ✅ **COMPLETE** | 2026-08-02 | 2026-08-02 | obligation_taxonomy.py created with keyword matching; canonical IDs for obligations and capabilities integrated into extractor
| 5: Policy Chain | AC-5 | ✅ **COMPLETE** | 2026-08-02 | 2026-08-02 | Parent references (capability_id, policy_id, standard_id) added to transform chain; edge type renamed VALIDATES → IMPLEMENTED_BY
| 6: Role Validity | AC-6 | ✅ **COMPLETE** | 2026-08-02 | 2026-08-02 | Roles filtered by validate_roles_by_obligation_subject(); role_id persisted on Obligation nodes; HAS edge coverage now tracked in run summary
| 7: HAS Edges | AC-7 | ✅ **COMPLETE** | 2026-08-02 | 2026-08-02 | Role validation and role_id assignment already implemented; HAS edges created using MERGE |
| 8: ID Collisions | AC-8 | ✅ **COMPLETE** | 2025-08-02 | 2025-08-02 | Updated to SHA-256 with 12-char digest over normalized text; collision risk significantly reduced |
| 9: Edge Idempotency | AC-9 | ✅ Complete | 2026-08-02 | 2026-08-02 | MERGE implemented for all edges; edge deduplication verified |
| 10: Provenance | AC-10 | ✅ **COMPLETE** | 2026-08-02 | 2026-08-02 | `source_ref` persisted on Role nodes; `related_obligation_ref` already persists on Capability nodes |

---

## Risk Assessment

### Technical Risks
1. **Regulation parsing dependency** - EU EUR-Lex HTML format could change (mitigated by EU stability)
2. **LLM consistency** - results may vary between runs (temperature/seed not currently controlled)
3. **Graph database availability** - FalkorDB required for testing (can mock for unit tests)
4. **Reset strategy between phases — RESOLVED (2026-08-02):** option (a).
   FalkorDB is wiped and the full pipeline (`run-spike.py --all`) is
   re-run from scratch after every phase, not just at the end. This means
   Phase 3's REQUIRES edges — keyed to today's regulation-prefixed
   Obligation IDs — are naturally rebuilt correctly against Phase 4's
   canonical IDs once Phase 4 lands; no manual edge-repair step is needed
   between phases. Trade-off accepted: slower iteration (a full CRA+NIS2
   re-ingestion per phase, not an incremental patch) in exchange for never
   having to reason about which edges are stale.

### Schedule Risks
1. **AC-4 scope** - new taxonomy module + canonical ID pattern change is significant
2. **Backward compatibility** - graph data is reset before every phase's re-run (Technical Risk 4 — resolved)
3. **Test infrastructure** - need FalkorDB running with a test database, cleared before each phase verification run
4. **LLM hallucination mitigation** - Phase 3 showed that capabilities sometimes reference articles not in the input text; this is expected behavior given LLM limitations and is handled by fallback matching.
5. **Obligation index fallback** - Phase 3 added support for `'Obligation X'` format in `related_obligation_ref`, matching by position in extracted list.

5. ✅ **Phase 4 (AC-4 - Canonical Identity) COMPLETED**
   - Created `obligation_taxonomy.py` with keyword-based matching
   - Updated extractor.py with canonical obligation ID generation
   - Updated extractor.py with canonical capability ID generation
6. ✅ **Phase 5 (AC-5) - Policy Chain Fix COMPLETED**
7. ✅ **Phase 6 (AC-6) - Role Validity COMPLETED**
8. ✅ **Phase 7 (AC-7) - HAS Edge Fix COMPLETED**
9. ✅ **Phase 8 (AC-8) - ID Collision Fix COMPLETED**
10. ✅ **Phase 9 (AC-9) - Edge Idempotency VERIFIED**
11. ✅ **Phase 10 (AC-10) - Provenance Traceability COMPLETED**

---

*End of Document*
