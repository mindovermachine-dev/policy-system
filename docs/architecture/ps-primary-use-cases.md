<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System — Primary Use Cases

**Status:** Draft

---

## Purpose

This document defines the primary use cases of the Policy System. It is the
foundation for the system architecture: components and interfaces are derived
from these use cases, not the other way around. Each use case is designed to
be independently valuable to a customer.

## Context

Architectural decisions preceding this document (session 2026-08-08):

- The Policy System is a **single-tenant, customer-deployed product**. There
  is no multi-tenant platform layer and no vendor-operated runtime — nothing
  runs on the vendor side.
- Vendor involvement in EU regulation content is a **supply chain**, not part
  of the system architecture. The product receives/works with regulation
  content; how the vendor prepared any shipped content is outside the system
  boundary.
- The user interacts with the system through an **AI harness** (Pi, Claude
  Code, OpenCode, GitHub Copilot, etc.) — the system is tool-backed, not a
  chat product.
- Licensing is contract-only; no technical enforcement mechanism.

## Primary Use Cases

### UC-1: Prepare an external regulation

**A user prepares an external regulation (roles, requirements, obligations,
and capabilities) for loading into the system.**

Preparation is a capability of the product itself. It may be largely manual
for the time being — the architecture defines the workflow (regulation text
in → structured draft → human review → approved content), while how much of
the drafting is automated is an implementation detail that can change without
affecting the architecture.

### UC-2: Load an external regulation

**A user loads an external regulation into the system.**

Loading applies prepared content to the knowledge graph. Content operations
are add/merge-only: loading never modifies or deletes existing customer data.

### UC-3: Ask questions about regulations and policies

**A user asks the system questions about a regulation or policy.**

Answers are faithful fact retrieval with full provenance back to source
regulation text — the system returns facts with their provenance chain; the
user's AI harness owns synthesis and narration. This use case spans both
content layers of the domain model (regulatory content and organizational
content), which is what makes it the architecturally central one.

### UC-4: Govern internal regulations

**A user governs internal regulations (roles, requirements, obligations, and
capabilities).**

Internal regulations (e.g. an Engineering Practices standard) use the same
domain model as external ones (`source_type: internal` — see
[ps-domain-concepts.md](../artifacts/ps-domain-concepts.md)) and flow through
the same model chain, converging on the same canonical Obligation and
Capability nodes as external regulations.

### UC-5: Govern policy, standard, and control content

**A user governs policy/standard/control content for regulations in the
system.**

This is the organizational-accountability layer: where capabilities become
owned commitments with review cycles, and where the graph becomes the
customer's own.

## Content Layers

The five use cases split by which half of the domain model they touch:

| Group | Use cases | Content layer |
|---|---|---|
| Regulatory content | UC-1, UC-2, UC-4 | Regulation → Role → Requirement → Obligation → Capability |
| Organizational content | UC-5 | Capability → Policy → Standard → Control |
| Spanning both | UC-3 | Full provenance chain |

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
