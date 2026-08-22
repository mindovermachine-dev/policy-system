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

**A Compliance Officer lists the available EU Regulations and from the list select and ingest one regulation, followed by selecting and ingesting another regulation resulting in the company graph having two eu regulations in a merged unified knowledge graph.**

Selecting a regulation kicks off the full ingestion pipeline for that
regulation: Regulatory Structural Ingestion (Stage 1) fetches and
structurally tags the regulation's text from Cellar/ELI; Baseline Curation
(Stage 2) maps it into the canonical, PS-domain-shaped baseline graph;
Company Merge (Stage 3) merges it into the company's single-tenant
regulatory graph. The result is a regulatory knowledge graph reflecting the selected
regulation(s), ready to query. Content operations are add/merge-only:
adding a regulation never modifies or deletes existing customer data.


### UC-2: Govern internal regulations

**A Policy Manager ingest a generic template of Engineering Practices into the company knowledge graph**

Internal regulations (e.g. an Engineering Practices standard) use the same
domain model as external ones (`source_type: internal` — see
[ps-domain-concepts.md](../artifacts/ps-domain-concepts.md)). This will populate the full compliance spine: the same Role/Requirement/Obligation/Capability chain as external regulations, continuing on to Policies, Standards and Control. Policies are mapped to Regulatory Capabilities where they already exist, or mint new Capabilities that link back to the internal Business Regulation.

### UC-3: Ask compliance questions

**An engineering manager uses the policy question skill in claud desktop to answer questions via ps service and falkordb holding the comany knowledge graph.**

This is the Policy System client layer: Whene an external client (Claude Desktop) interface with the Policy System container to answer questions about EU Regulations and Business policies


---

*End of Document*
