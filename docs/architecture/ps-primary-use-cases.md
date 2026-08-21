<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System — Primary Use Cases

**Status:** Draft

---

## Purpose

This document defines the primary use cases of the Policy System. It is the
foundation for the system architecture: components and interfaces are derived
from these use cases, not the other way around. Each use case is designed to
be independently valuable to a customer.

## Primary Use Cases

### UC-1: Select and add a regulation to the system

**A user lists the EU regulations available via Cellar/ELI and selects one
to add to the system.**

Selecting a regulation kicks off the full ingestion pipeline for that
regulation: Regulatory Structural Ingestion (Stage 1) fetches and
structurally tags the regulation's text from Cellar/ELI; Baseline Curation
(Stage 2) maps it into the canonical, PS-domain-shaped baseline graph;
Company Merge (Stage 3) merges it into the company's single-tenant
regulatory graph. The result is a regulatory graph reflecting the selected
regulation(s), ready to query. Content operations are add/merge-only:
adding a regulation never modifies or deletes existing customer data.

### UC-2: Govern internal regulations

**A user governs internal regulations (roles, requirements, obligations, and
capabilities).**

Internal regulations (e.g. an Engineering Practices standard) use the same
domain model as external ones (`source_type: internal` — see
[ps-domain-concepts.md](../artifacts/ps-domain-concepts.md)) and flow through
the same model chain, converging on the same canonical Obligation and
Capability nodes as external regulations.

### UC-3: Govern policy, standard, and control content

**A user governs policy/standard/control content for regulations in the
system.**

This is the organizational-accountability layer: where capabilities become
owned commitments with review cycles, and where the graph becomes the
customer's own.

## Content Layers

The three use cases split by which half of the domain model they touch:

| Group | Use cases | Content layer |
|---|---|---|
| Regulatory content | UC-1, UC-2 | Regulation → Role → Requirement → Obligation → Capability |
| Organizational content | UC-3 | Capability → Policy → Standard → Control |

## Open Questions

*Deferred from the use-case discussion; to be resolved during architecture
work or later.*

1. Delta-only updates: loading is add/merge-only today — how superseded
   content is retired (domain model has `SUPERSEDED_BY` / `deprecated`
   statuses) is not yet specified.
2. Extraction provenance: whether metadata about how content was prepared
   (methodology, tooling versions) is attached to prepared content or kept
   outside the product. Current decision: does not travel with the content.

---

*End of Document*
