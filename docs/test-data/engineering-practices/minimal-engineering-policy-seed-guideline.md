<!-- © 2026 Cartman ApS. All rights reserved. -->
# Minimal Engineering Policy Seed Guideline

**Scope:** Mixed software + regulated domains  
**Baseline style:** Enforced-only, balanced policy granularity  
**Purpose:** Define a complete but minimal starter dataset importable into the Policy System graph.

---

## 1. Definition of Minimal Complete

A starter set is **minimal complete** when every major engineering risk path has:
- at least one governing `Policy`
- at least one verifiable `Control` path

Required risk paths:
1. Secure build and release
2. Reliable service operation
3. Data protection and privacy
4. Traceability and auditability
5. Third-party and supply chain risk
6. Incident and recovery readiness

If a policy does not clearly reduce one of these paths, it is likely non-minimal.

---

## 2. Engineering Practice Areas

Recommended baseline areas (10):
1. Engineering Governance and Exceptions
2. Secure Development Lifecycle
3. Identity and Access for Engineering Systems
4. Change, Release, and Deployment Safety
5. Software Supply Chain and Dependencies
6. Quality Engineering and Test Assurance
7. Reliability and Service Operations
8. Vulnerability and Incident Management
9. Data Protection and Privacy Engineering
10. Observability, Logging, and Audit Evidence

---

## 3. Typical Minimal Policy Set (Enforced Only)

Recommended baseline policies (12):
1. Engineering Policy Governance
2. Risk Acceptance and Exception Management
3. Secure SDLC Policy
4. Access Control for Engineering Tooling
5. Change and Release Control
6. Deployment Safety and Rollback Policy
7. Third-Party Component and License Policy
8. Build Integrity and Artifact Provenance Policy
9. Test and Quality Gate Policy
10. Reliability and Incident Response Policy
11. Vulnerability Management and Remediation Policy
12. Data Handling, Logging, and Audit Retention Policy

This set is intentionally balanced: not too broad, not too fragmented.

---

## 4. Policy System Graph Seed Shape

Recommended initial dataset size:
1. `Regulation` (internal): 1
2. `Role`: 5-7
3. `Requirement`: 20-30
4. `Obligation`: 15-20
5. `Capability`: 12-16
6. `Policy`: 12
7. `Standard`: 12-18
8. `Control`: 24-36

This is typically the smallest dataset that still produces useful graph convergence and traversal.

---

## 5. Minimality Rule

When new requirements appear:
1. First map to existing `Obligation` and `Capability`
2. Only add a new `Policy` if owner, governance cadence, or control model genuinely differs

This prevents policy sprawl and preserves canonical reuse.

---

## 6. Completeness Checks Before Import

1. Every `Requirement` has a path to at least one `Control`
2. Every `Capability` is governed by exactly one active `Policy`
3. Every `Policy` has at least one `Standard` and one active `Control`
4. Every critical role has at least one assigned `Obligation`
5. Most engineering-heavy controls are automated (target 60-70%)
6. No policy groups unrelated capabilities with different ownership models

---

## 7. Common Failure Modes

1. One policy per regulation article
2. Practice areas that are too narrow
3. Using standards as policy substitutes
4. Missing explicit exception handling policy
5. Controls without evidence references or review cadence

---

## 8. Phased Seed Strategy

1. Phase 1: 12 policies, one standard each, one automated control each
2. Phase 2: Add manual governance controls and role-specific standards
3. Phase 3: Map external regulations to existing obligations/capabilities before adding new nodes

This sequence gives fast value while keeping the baseline lean.
