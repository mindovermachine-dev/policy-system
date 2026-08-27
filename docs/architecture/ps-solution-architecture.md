<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System Solution Architecture

**Status:** Draft  
**Project Name:** Policy System  
**Last Updated:** August 21st 2026

---

## Table of Contents

1. [Overview](#overview)
2. [C4 System Context Level](#c4-system-context-level)
   - [Diagram Legend & Conventions](#diagram-legend--conventions)
   - [C4 System Context Diagram](#c4-system-context-diagram)
   - [System Context Breakdown](#system-context-breakdown)
3. [C4 Container Level](#c4-container-level)
   - [C4 Container Diagram](#c4-container-diagram)
   - [Container Breakdown](#container-breakdown)
   - [User Role Mapping](#user-role-mapping)
4. [C4 Component Level](#c4-component-level)
   - [Component Breakdown](#component-breakdown)
5. [Use Case Coverage Mapping](#use-case-coverage-mapping)
   - [Query/Read-Only Roles Use Cases](#queryread-only-roles-use-cases)
   - [Regulation Ingestion Use Cases](#regulation-ingestion-use-cases)
   - [Regulatory Change Monitoring Use Cases](#regulatory-change-monitoring-use-cases)
   - [Policy Managers Use Cases](#policy-managers-use-cases)
   - [System Admin Use Cases](#system-admin-use-cases)
6. [NFR Realization](#nfr-realization)
7. [Architectural Considerations](#architectural-considerations)
   - [Risks & Concerns](#risks--concerns)

---

## Overview

**System Purpose & Scope**
The purpose of the Policy System is to provide a backend service that ingests, stores, and monitors EU regulations in a compliance knowledge graph, unifying a select set of EU regulations with a company's internal business regulations and policies. The service will expose a REST API for lifecycle/config and authoring clients (PS-Cli, Policy Editor), and an MCP interface exposing a Cypher query mechanism for PS Question Skill to query the knowledge graph.



**Key Capabilities**
- Ingest EU Regulations via Cellar/ELI
- Map raw EU regulation into the PS Conceptual Model (`ps-domain-concepts.md`) knowledge graph
- Monitor ingested EU regulations for amendments and trigger re-ingestion of affected content
- Ingest Business regulation and policies, minting Capability nodes plus governance-layer Policy/Standard/Control nodes
- Cypher Query interface

**Consuming clients**
- PS-Cli a command line interface for starting / stopping and configuring the PS Service, and for driving regulation ingestion — selecting EU regulations for Cellar/ELI-sourced ingestion, and a PDF ingestion pipeline for business regulations/policies (under exploration)
- PS Question Skill a skill that allows agents in VSCode or Claude Desktop ask questions, send Cypher queries to the PS Service and articulate answers to the users question
- Policy Editor a client for authoring a Policy/Standard/Control from scratch and linking it to an existing Capability (under exploration — client not yet designed)

**Deployment Architecture**
- The PS Service is deployed as a Container and can be run in Podman, Kubernetes etc.
- The PS Service depends on FalkorDB which should be deployed in a separate container. This allows for patching FalkorDB without rebuilding and deploying the PS Service.
- PS Service is single-tenant

**Regulatory Compliance (if applicable)**
- EU GDPR
- EU NIS2

---

## C4 System Context Level

### Diagram Legend & Conventions

**Shape Conventions:**
- **Hexagon shapes:** External systems and actors — consuming clients and external platform dependencies outside the Policy System boundary
- **Rectangle shapes:** Internal containers within the Policy System
- **Cylinder shapes:** Data stores

**Color Coding:**
- **Yellow (`#FFD54F`) fill:** External platform/data service dependency (e.g., Cellar/ELI)
- **Blue (`#90CAF9`) fill:** External consuming client application (e.g., PS-Cli, PS Question Skill)
- **Teal (`#4DB6AC`) fill:** Core Policy System service (PS Service)
- **Green (`#81C784`) fill:** Internal data store container (e.g., FalkorDB)

**Architectural Significance:**
- External system interactions (Cellar/ELI) represent integration boundaries with third-party data platforms outside the team's control
- Consuming clients (PS-Cli, PS Question Skill) interact with PS Service exclusively through its REST/Cypher API — no direct data-store access
- FalkorDB is deployed as a separate container from PS Service specifically so it can be patched independently without rebuilding or redeploying PS Service

### C4 System Context Diagram

```mermaid
graph TB
    subgraph Clients["Consuming Clients"]
        PSCli{{PS-Cli}}
        PSSkill{{PS Question Skill}}
        PolicyEditor{{Policy Editor}}
    end

    subgraph Platform["External Platform Services"]
        Cellar{{Cellar/ELI<br/>EU Regulatory Feed}}
        LLMProvider{{LLM Provider<br/>via LiteLLM}}
    end

    subgraph System_Name["Policy System"]
        MainSystem[PS Service]
    end

    PSCli -->|"REST: lifecycle/config; regulation ingestion (Cellar/ELI selection; PDF pipeline under exploration)"| MainSystem
    PSSkill -->|"MCP: submit query"| MainSystem
    MainSystem -->|"Query results"| PSSkill
    PolicyEditor -.->|"REST: author policy/standard/control (under exploration)"| MainSystem

    Cellar -->|"Regulatory text, structure, ELI citations, amendment history"| MainSystem
    MainSystem -->|"Chat/embedding completions via LiteLLM"| LLMProvider

    style PSCli fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333
    style PSSkill fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333
    style PolicyEditor fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333,stroke-dasharray: 5 5
    style Cellar fill:#FFD54F,stroke:#333,stroke-width:2px,color:#333
    style LLMProvider fill:#FFD54F,stroke:#333,stroke-width:2px,color:#333
    style MainSystem fill:#4DB6AC,stroke:#333,stroke-width:2px,color:#FFFFFF
```

*(See Diagram Legend & Conventions above for shape and color interpretations. Dashed border = under exploration, not yet designed.)*

### System Context Breakdown

| Actor | Interaction | System | Actor/System Type | Objective |
| :--- | :--- | :--- | :--- | :--- |
| Cellar/ELI | PS Service fetches EU regulatory document structure (TITLE/CHAPTER/SECTION/ARTICLE/PARAGRAPH), verbatim text per element, ELI citation identity, and amendment history | Policy System | External platform service (EU Publications Office) | Ingest authoritative, structurally-addressable EU regulatory text to keep the compliance knowledge graph current, replacing non-deterministic PDF extraction as the source of truth |
| LLM Provider | PS Service sends prompts/text for chat and embedding completions (provider-agnostic via LiteLLM: Ollama, Azure Foundry, AWS Bedrock, Anthropic, OpenAI, …) and receives generated text/embeddings back | Policy System | External platform service (LLM provider, swappable) | Enable LLM-assisted curation of regulatory/business content into the PS Conceptual Model, without coupling the system to a single LLM vendor |
| PS-Cli | Starts, stops, and configures PS Service via its REST API. Also selects EU regulations for Cellar/ELI-sourced ingestion (UC-1), and drives a PDF ingestion pipeline (single file or folder) for business regulations/policies producing proposed Policy/Standard/Control content and suggested edge links to existing Capabilities — **pipeline design is under exploration, not yet specified** | Policy System | External client application (command-line interface) | Enable operators to manage the PS Service lifecycle/configuration, trigger EU regulation ingestion via Cellar/ELI, and enable bulk ingestion of business regulation/policy documents into the governance layer |
| PS Question Skill | Sends Cypher queries to PS Service's MCP Interface and receives results, used to articulate answers to user questions | Policy System | External client application (Claude Desktop / VS Code skill) | Enable users in their agentic coding/chat environment to ask natural-language questions about regulations and policies, answered against the compliance knowledge graph |
| Policy Editor | Enables a user to author a Policy/Standard/Control from scratch and link it via an edge to an existing Capability — **client and interaction design are under exploration, not yet specified** | Policy System | External client application (not yet designed) | Enable manual authoring of governance-layer content as an alternative to PDF-based ingestion |



---

## C4 Container Level

### C4 Container Diagram

```mermaid
graph TB
    subgraph Clients["Consuming Clients"]
        PSCli{{PS-Cli}}
        PSSkill{{PS Question Skill}}
        PolicyEditor{{Policy Editor}}
    end

    subgraph Platform["External Platform Services"]
        Cellar{{Cellar/ELI<br/>EU Regulatory Feed}}
        LLMProvider{{LLM Provider<br/>via LiteLLM}}
    end

    subgraph System_Name["Policy System"]
        PSService[PS Service<br/>REST API + Cypher query interface<br/>Deployed as a container]
        FalkorDB[(FalkorDB<br/>Graph database<br/>Deployed as a separate container)]

        PSService -->|"Cypher: read/write knowledge graph"| FalkorDB
    end

    PSCli -->|"REST: lifecycle/config; regulation ingestion (Cellar/ELI selection; PDF pipeline under exploration)"| PSService
    PSSkill -->|"MCP: submit query"| PSService
    PSService -->|"Query results"| PSSkill
    PolicyEditor -.->|"REST: author policy/standard/control (under exploration)"| PSService

    Cellar -->|"Regulatory text, structure, ELI citations, amendment history"| PSService
    PSService -->|"Chat/embedding completions via LiteLLM"| LLMProvider

    style PSCli fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333
    style PSSkill fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333
    style PolicyEditor fill:#90CAF9,stroke:#333,stroke-width:2px,color:#333,stroke-dasharray: 5 5
    style Cellar fill:#FFD54F,stroke:#333,stroke-width:2px,color:#333
    style LLMProvider fill:#FFD54F,stroke:#333,stroke-width:2px,color:#333
    style PSService fill:#4DB6AC,stroke:#333,stroke-width:2px,color:#FFFFFF
    style FalkorDB fill:#81C784,stroke:#333,stroke-width:2px,color:#333
```

*(See Diagram Legend & Conventions above for shape and color interpretations. Dashed border = under exploration, not yet designed.)*

### Container Breakdown

**Important:** Diagrams must accurately reflect the containers listed in this table. The table is the source of truth; diagrams are visual representations of this data.

| Container/Service | Description | Key Responsibilities | Domain path |
|-----|---|---|---|
| PS Service | Backend REST API service, deployed as a single-tenant container (Podman/Kubernetes) | • Expose REST API for consuming clients (PS-Cli, Policy Editor) and an MCP interface for PS Question Skill<br/>• Ingest EU regulations via Cellar/ELI<br/>• Map raw EU regulation text into the PS Conceptual Model knowledge graph<br/>• Ingest business regulations/policies through the same regulatory-spine pipeline as external regulations, minting Capability nodes plus the governance-layer Policy, Standard, and Control nodes those internal sources require<br/>• Expose a Cypher query interface to consuming services<br/>• Persist and query the compliance knowledge graph via FalkorDB<br/>• Access an LLM Provider via LiteLLM for content curation | ps.service |
| FalkorDB | Graph database storing the compliance knowledge graph, deployed as a separate container from PS Service to allow independent patching without rebuilding/redeploying PS Service | • Store the regulatory and organizational layers of the compliance knowledge graph<br/>• Execute Cypher queries issued by PS Service | ps.falkordb |



### User Role Mapping

| User Role | Accessible Containers/Services |
|-----------|-------------------------------|
| Compliance Officers | PS Service via PS Question Skill (read-only query) |
| Policy Managers | PS Service via Policy Editor (create/edit/approve policies/standards — under exploration) |
| Legal Counsel | PS Service via PS Question Skill (read-only query) |
| Security Architects | PS Service via PS Question Skill (read-only query) |
| Risk Managers | PS Service via PS Question Skill (read-only query) |
| DevOps/Engineering | PS Service via PS Question Skill (VS Code) |
| Auditors | PS Service via PS Question Skill (read-only query) |
| Software Engineers | PS Service via PS Question Skill (read-only query) |
| Security Engineers | PS Service via PS Question Skill (read-only query) |
| Engineering Managers | PS Service via PS Question Skill (read-only query) |
| System Admin | PS Service via PS-Cli (lifecycle/config; PDF ingestion pipeline — under exploration) |

**Purpose:** Maps user roles to the containers/services they can access, clarifying access control boundaries.

---

## C4 Component Level

### Component Breakdown

| Component | Description | Key Responsibilities | Domain path |
| :--- | :--- | :--- | :--- |
| Ingestion | Ingests regulation content from a source (Cellar/ELI for EU regulations) instead of PDF, persisting it as a native structural graph | Fetch a regulation's structure/text via a source-specific Ingestion Adapter; persist that source's native structural graph to FalkorDB as-is — see Container Architecture for the adapter pattern | ps.service.ingestion |
| Domain Mapper | Maps a regulation's native structural graph into the curated, PS-domain-shaped baseline graph — Role/Requirement/Obligation/Capability for external sources; continuing through Policy/Standard/Control for internal sources | Curate/derive PS Conceptual Model entities from a regulation's native structural graph, read via a Domain Mapping Adapter paired to the source's Ingestion Adapter, into a per-regulation baseline. The adapter determines depth: external adapters stop at Capability, the internal-source adapter continues to Control | ps.service.domainmapper |
| Company Merge | Dedupes and merges a company's selected regulation baseline graphs into one single-tenant graph | Merge selected baseline graphs per company — operates only on the per-regulation baseline graph Domain Mapper produces, resolving it into the canonical, cross-regulation-deduped company graph; unaffected by source-specific (Cellar/ELI) adapter changes | ps.service.companymerge |
| Query Engine | Executes Cypher queries against the compliance knowledge graph on behalf of PS Question Skill and returns results | Receive Cypher queries via PS Service's query interface; execute against FalkorDB; return results with provenance | ps.service.queryengine |
| MCP Interface | Exposes the compliance knowledge graph's Cypher query capability to PS Question Skill via the Model Context Protocol (MCP) | Accept MCP tool calls from PS Question Skill; delegate query execution to Query Engine; return results over MCP | ps.service.mcpinterface |
| Regulatory Change Monitor | Detects when an EU regulation is amended by polling Cellar/ELI, and triggers re-ingestion of the affected regulation | Poll Cellar/ELI for regulatory amendments; trigger a new Ingestion → Domain Mapper → Company Merge cycle for the changed regulation; surface a delta report of affected content — **delta report shape/mechanism is under exploration** | ps.service.changemonitor |
| LLM Interface | Shared internal component wrapping LiteLLM, giving other components a single point of access to the configured LLM Provider | Route chat/embedding requests to the configured LLM Provider via LiteLLM; abstract provider-specific credentials/config away from consuming components (Domain Mapper, Company Merge, and potentially Query Engine/Regulatory Change Monitor) | ps.service.llminterface |
| Logging | Shared internal component providing structured, semantic logging for every other PS Service component, giving detailed debug data during pipeline/query runs | Accept structured log entries from other components; write JSON-structured entries to the configured log sink; bind a correlation (run) ID at each primary-use-case entry point so a full pipeline/query run can be traced end to end | ps.service.logging |

---

## Use Case Coverage Mapping

Use cases reference `ps-primary-use-cases.md`'s URS IDs (UC-1 through UC-4).
Roles are grouped where they share the same use case and implementing
component(s), rather than listed individually.

### Query/Read-Only Roles Use Cases

Compliance Officers, Legal Counsel, Security Architects, Risk Managers,
DevOps/Engineering, Auditors, Software Engineers, Security Engineers,
Engineering Managers.

| Use Case | API Entry Point | Implementing Container/Component | URS Requirement |
| :--- | :--- | :--- | :--- |
| Query regulations and policies | PS Question Skill → PS Service MCP Interface | MCP Interface (delegates to Query Engine) | UC-3 |

### Regulation Ingestion Use Cases

| Use Case | API Entry Point | Implementing Container/Component | URS Requirement |
| :--- | :--- | :--- | :--- |
| Select and add a regulation | PS-Cli → PS Service | Ingestion → Domain Mapper → Company Merge | UC-1 |
| Govern internal regulations | PS-Cli → PS Service | Ingestion → Domain Mapper → Company Merge (same pipeline as UC-1, `source_type: internal`; Domain Mapper continues past Capability through Policy/Standard/Control via the internal-source adapter) | UC-2 |

### Regulatory Change Monitoring Use Cases

Not user-triggered — Regulatory Change Monitor initiates this use case autonomously by polling Cellar/ELI.

| Use Case | API Entry Point | Implementing Container/Component | URS Requirement |
| :--- | :--- | :--- | :--- |
| Automatically detect and absorb a regulatory amendment | Regulatory Change Monitor → Cellar/ELI (poll) | Regulatory Change Monitor (triggers) → Ingestion → Domain Mapper → Company Merge | UC-4 |

### Policy Managers Use Cases

| Use Case | API Entry Point | Implementing Container/Component | URS Requirement |
| :--- | :--- | :--- | :--- |
| Govern policy/standard/control content | Policy Editor → PS Service (under exploration) | Policy Editor backend (not yet defined) | — (not yet defined in `ps-primary-use-cases.md`) |

### System Admin Use Cases

| Use Case | API Entry Point | Implementing Container/Component | URS Requirement |
| :--- | :--- | :--- | :--- |
| Govern policy/standard/control content (bulk PDF path) | PS-Cli → PS Service (under exploration) | PDF ingestion pipeline (not yet defined) | — (not yet defined in `ps-primary-use-cases.md`) |

**Purpose:** Provides traceability from user requirements (URS) to architectural implementation, ensuring all use cases are covered by the architecture.

---

## NFR Realization

This section maps non-functional requirements (from URS) to architectural decisions that satisfy them.

| NFR ID | Requirement Summary | Architectural Decision | Containers Affected | Rationale |
|--------|--------------------|-----------------------|---------------------|-----------|
| [NFR-PERF-001] | [e.g., P95 latency < 200ms] | [e.g., Redis caching layer for read-heavy endpoints] | [e.g., API, Cache] | [Why this decision satisfies the NFR] |

**Guidelines:**
- Every NFR from the URS should appear here — if an NFR has no architectural decision, document why (e.g., "deferred to component-level implementation")
- Multiple NFRs may share an architectural decision
- Decisions here drive Container Architecture strategies (CA documents the component-level details)

---

## Architectural Considerations

### Risks & Concerns

| Risk Category | Description | Mitigation Strategy |
|---------------|-------------|---------------------|
| Design Gap | Two business/policy ingestion paths (PS-Cli's PDF ingestion pipeline; the Policy Editor client) are named in the System Context but their design is under exploration — pipeline mechanics, proposal/review workflow, and the Policy Editor client itself are all unspecified | To be resolved through further design exploration before Container/Component-level detail is added for either path |
| Security | Authentication/authorization for PS Service's REST API is out of scope for now — no identity provider or auth mechanism is defined | Deferred; must be decided before any multi-user or production deployment |
| Security | LLM-driven extraction (Domain Mapper) treats ingested regulatory/SoP source text as trusted input to the extraction prompt — adversarial content in a Cellar/ELI response or an internal SoP document could attempt to manipulate extraction into minting misleading Obligation/Capability/Policy/Standard/Control content, which the add/merge-only design would then retain | Not yet addressed; requires design exploration, particularly before internal-source (Business SoP) ingestion is treated as production-ready |
| Security | Credential/secrets management for LLM Provider API keys and FalkorDB access is undecided — no storage, rotation, or handling mechanism is specified anywhere in the architecture | To be decided at L2/implementation; must be confirmed before any non-local deployment |
| Security | No encryption at rest or in transit is specified for FalkorDB, which stores the full compliance knowledge graph — a company's regulatory gaps and control posture in one place | Deferred; should be decided before any non-local/production deployment, alongside the REST/MCP auth decision above |
| Security | Logging is operational/pipeline tracing (`run_id` correlation), not a tamper-evident security audit trail of who accessed or mutated what — relevant once REST/MCP auth introduces distinct callers to track | Deferred; revisit alongside the auth decision above |
| Technical Debt | Query Engine's guard/execution logic (`ExecuteCypherQuery`) now lives under `query_engine/`, matching the module boundary Container Architecture assigns it, but MCP Interface still invokes it out-of-process, via `subprocess`, rather than in-process | Switch MCP Interface to an in-process call before additional functionality is built on top of the current subprocess boundary |
| Scalability Bottleneck | No action in the architecture has a load-tested SLA — Query Engine's one stated target ("< 2s") is explicitly draft/not load-tested, and every other action's Processing Time is unset or best-effort | Load-test the read path and establish real SLOs before relying on any stated number to drive timeout/capacity decisions |
| Technical Debt | LLM extraction (Domain Mapper) is explicitly non-deterministic, yet Role/Requirement/Obligation/Capability identity is content-hash-derived — a retried run after a partial failure could reword the same source text differently and mint a duplicate instead of matching the existing node | Not yet addressed; requires design exploration into retry-safe extraction or a within-regulation semantic-match fallback, not just Company Merge's cross-regulation convergence |
| Design Gap | Company Merge surfaces (rather than resolves) a low-confidence semantic-match candidate, aborting the merge — no review/resolution workflow is defined for that surfaced state, and Regulatory Change Monitor can trigger this unattended (UC-4) | To be resolved through further design exploration — needs an owner, a queue/notification mechanism, and a decision on whether ingestion stays blocked pending review |
| Technical Debt | No schema/data migration strategy exists for the graph itself — Regulation instances version explicitly, but nothing addresses what happens to already-minted nodes when `ps-domain-concepts.md`'s own shape changes (a property renamed or added) | Deferred; needs a decision before the domain model is revised against a graph that already holds data |
| Coverage Gap | UC-1 (Select and add a regulation to the system) — no human role is defined as the one who lists/selects a regulation to trigger ingestion. UC-2 (Govern internal regulations — Role/Requirement/Obligation/Capability for internal content) has no assigned role or component; neither PS-Cli's pipeline nor Policy Editor currently targets this shape | To be resolved through further design exploration; must be assigned before Use Case Coverage Mapping is complete |

**Common Risk Categories:**
- **Single Points of Failure:** Components or systems whose failure would cause system-wide issues
- **Scalability Bottlenecks:** Areas that may not scale under increased load
- **Security Considerations:** Authentication, authorization, data protection, and compliance concerns
- **Integration Complexity:** Challenges with external system dependencies and data synchronization
- **Technical Debt:** Known limitations, legacy patterns, or areas requiring future refactoring

---

**End of Document**

---

