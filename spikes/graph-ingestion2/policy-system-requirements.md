# Policy System Requirements

### Functional Requirements by User Priority

**FR01: Regulation Ingestion & Obligation Extraction** (Compliance Officers, Legal Counsel)
- Parse regulatory texts (CRA, NIS2, GDPR, etc.) to extract obligations as structured entities
- Identify obligation types: requirements, prohibitions, recommendations
- Extract jurisdiction, effective dates, and associated regulation articles
- Support versioning when regulations are updated

**FR02: Policy Content Management** (Policy Managers)
- Create, edit, and approve business policies and standards (CRUD operations)
- Define internal organization standards that map to obligations
- Manage lifecycle of business policies

**FR03: Knowledge Graph Construction & Relationship Mapping** (All roles - foundational capability)
- Model obligations, policies, controls, standards as graph nodes
- Define relationship types: `FULFILLS`, `REQUIRES`, `ALIGNS_WITH`, `SUPPLEMENTS`
- Enable manual and semi-automated curation of relationships between obligations and org responses
- Support bidirectional traversal (obligation → policy/control AND control → obligation)

**FR04: Natural Language Query with Graph Enrichment** (DevOps/Engineering, Compliance Officers, Risk Managers)
- Accept questions in natural language about regulatory requirements
- Combine semantic search on text descriptions with graph pattern matching
- Return answers showing the query-graph path (e.g., "Obligation 1.2.3 is addressed by Policy X, implemented via Control Y")
- Support follow-up queries for deeper traversal

**FR05: Gap Analysis & Compliance Scoring** (Risk Managers, Compliance Officers, Legal Counsel)
- Identify regulatory obligations without organizational policies/controls mapped
- Calculate compliance coverage score per regulation, policy, or control domain
- Show control effectiveness gaps and redundancy
- Provide drill-down by obligation, policy, standard, and control

**FR06: Control Lifecycle Management** (Security Architects, Policy Managers)
- Track control implementation status (planned, implemented, reviewed, deprecated)
- Link controls to testing evidence and audit findings
- Manage control owners and review dates

**FR07: Dynamic Update System & Impact Assessment** (Compliance Officers, Legal Counsel, Risk Managers)
- When a regulation changes, identify which obligations are affected
- Show impact assessment: policies/controls that fulfill those obligations need review
- Support 'what-if' scenarios for regulatory change impact

**FR08: Governance Workflow Management** (Compliance Officers)
- Capture approval workflows and decision logs as executable content
- Define review cycles, stakeholder assignments, escalation paths
- Maintain audit trail of governance decisions

**FR09: Confidence Threshold for Auto-Approval** (LLM Processing System)
- LLM-generated obligations, policies, standards, controls require ≥90% confidence (configurable) based on evidence linking back to original regulation
- Below threshold triggers human escalation

**FR10: Human Escalation Package** (Compliance Officers, Legal Counsel)
When escalating for review, system must provide:
- Original regulation reference (line number or section)
- Proposed obligation/policy/standard/control
- Rubric score and reasoning for the score

**FR11: Full Audit Trail** (All roles - cross-cutting requirement)
The system must record for all actions:
- LLM reasoning chain (prompts, tool calls, intermediate output including internal thinking)
- Timestamps of all actions
- Human approvals and modifications made during review
- This applies to all functional requirements (FR01-FR10) as a cross-cutting concern

**FR12: Async Ingestion Queue with Delta Tracking** (System Infrastructure)
- All ingestion work must be queued and processed asynchronously
- When regulations update, track delta changes to affected obligations/policies/standards/controls
- Flag what changed clearly for downstream impact assessment

**FR13: Conflict Detection and Flagging** (Risk Managers, Compliance Officers)
- Detect conflicting obligations across regulations
- Do NOT block processing of conflicting content
- Create separate entries for each conflicting obligation
- Flag conflicts for human review

### Non-Functional Requirements

**NFR01: Auditability & Immutable Logging**
- The system must have fully auditable immutable logs of any system action including LLM input, tool calls and outputs including but not limited to internal thinking and evaluations.
- This applies to all functional requirements (FR01-FR13) as a cross-cutting concern

**NFR02: Multi-Jurisdiction Support**
- The system must be able to ingest any EU regulation using LLMs to map it into an internal generic regulator model
- The system should handle worldwide regulations from the US and other countries just as EU regulations

**NFR03: Response Latency** (Query & Check Layer)
- All query and check operations must respond within 50ms under normal load

**NFR04: Version Retention Policy**
- All entities (obligations, policies, standards, controls) use immutable IDs
- Previous versions retained only for historical/audit purposes
- Policies always reference latest version
- Default retention: 5 years (configurable)




