# Policy System

[//]: # (Table of Contents)

- [Governance Mechanism: How Decisions Are Made](#1-governance-mechanism-how-decisions-are-made)
- [Policy Content: What Is Being Managed](#2-policy-content-what-is-being-managed)
- [Query & Check Mechanism: Interaction Layer](#3-query--check-mechanism-interaction-layer)
- [System Architecture Overview](#system-architecture-overview)
- [Key Objectives](#key-objectives)
- [Target Audiences](#target-audiences)
- [Target Outcomes](#target-outcomes)
- [Key Differentiators](#key-differentiators)

---

There is a wave and movement away from US based tech dependency towards EU.

EU has a heavy set of regulations that companies must manage and handle, and where they before were guidance they are now becoming obligations.

The heavy regulation is perceived as a problem because our compliance processes are largely manual and bolt on, but imagine if we could turn the EU regulations into a moat for EU based companies by automating and streamlining compliance work such that it becomes a competitive advantage?

## Vision

 By 2028 turn regulatory compliance from a manual burden into an automated competitive advantage for EU-based organizations — where every regulation and policy is instantly understood, mapped to business operations, and verified through systems that make compliance invisible yet inviolable. 

## Executive Summary

The **Policy System** is composed of three interconnected components that work together to transform regulatory compliance from a manual burden into an automated, business-enabling capability, intended to serve a single legal entity:

---

### 1. Governance Mechanism: How Policy Decisions Are Made

This is the *process layer* that defines how organizations coordinate around regulations and policies. It answers questions like:
- Who needs to review new regulations?
- When are policy updates approved?
- How are controls validated against obligations?
- What's the escalation path for compliance gaps?

While some governance activities occur in meetings and business processes, this component documents the *rules of engagement* as digital content within the system: approval workflows, review cycles, stakeholder assignments, decision logs.

**Key capability**: The system captures both the *process* (who does what when) and the *audit trail* (what decisions were made and why).

---

### 2. Policy Content: What Policies are being Managed

This is the *data layer* containing all regulatory and organizational content:

- **External Regulations**: Ingested from official EU sources (CRA, NIS2, GDPR, DORA, AI Act, etc.) - read-only, version-controlled
- **Obligations**: Extracted structured elements from regulations ("Article X.Y: shall implement technical measures...")
- **Business Policies**: Organizational commitments and intents - created and maintained by authorized users
- **Standards**: Implementation guidelines that fulfill policies
- **Controls**: Technical/mechanical checks that validate standards

The system builds a semantic knowledge graph linking obligations → policies → standards → controls, enabling traceability and gap analysis.

**Key capability**: Dynamic ingestion using LLMs to map any regulation into an internal generic regulator model, with delta-only updates when regulations change.

---

### 3. Query & Check Mechanism: Interaction Layer

This is the *application layer* that enables users and AI agents to interact with the system:

- **Natural Language Queries**: "What compliance requirements apply to our data encryption practices?"
- **Situation Understanding**: "Is this new API endpoint compliant with GDPR Article 32?"
- **Automated Control Checks**: CI/CD pipeline validation before promotion between environments
- **Gap Analysis**: "Which obligations lack coverage in our current control set?"

The system combines semantic search on text descriptions with graph pattern matching, returning answers with full provenance (showing the path from obligation → policy/control).

**Key capability**: Real-time answers with audit trail of how conclusions were derived.

---

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         GOVERNANCE LAYER                            │
│  • Approval workflows                                               │
│  • Review cycles & stakeholder assignments                          │
│  • Decision logs & escalation paths                                 │
└─────────────────────────────────────────────────────────────────────┘
                              ▲
                              │ defines/activates
                              │
┌─────────────────────────────┴───────────────────────────────────────┐
│                       POLICY CONTENT LAYER                          │
│  • External Regulations (read-only, versioned)                      │
│  • Obligations (extracted from regulations)                         │
│  • Business Policies (created/managed by org)                       │
│  • Standards (policy implementations)                               │
│  • Controls (technical validation checks)                           │
│  • Semantic knowledge graph linking all elements                    │
└─────────────────────────────┬───────────────────────────────────────┘
                              │ queried by
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       QUERY & CHECK LAYER                           │
│  • Natural language questions                                       │
│  • Situation understanding queries                                  │
│  • Automated compliance checks (CI/CD integration)                  │
│  • Gap analysis & compliance scoring                                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Objectives

1. **Governance**: Capture approval workflows and decision logs as executable content in the system
2. **Content Ingestion**: Enable natural language queries across obligations, policies, standards, controls
3. **Traceability**: Show *which* obligation drives *which* policy/standard/control and *how* they're linked
4. **Dynamic Updates**: Support changes to regulations without full re-ingestion (delta-only updates)
5. **Automation**: Enable both human queries and automated control checks (CI/CD pipeline integration)
6. **Auditability**: Maintain full auditable, immutable logs of all system actions including LLM inputs, tool calls, and reasoning

---

## Target Audiences

| Role | Primary Use Case |
|------|-----------------|
| **Compliance Officers** | Define governance processes; review regulations; query obligations and see mapped policies/controls; identify gaps |
| **Policy Managers** | Create, edit, and approve business policies and standards; manage content lifecycle |
| **Legal Counsel** | Review regulatory requirements and organizational responses; evaluate coverage gaps |
| **Security Architects** | See technical controls mapped to obligations they fulfill; design compliant solutions |
| **Risk Managers** | Get compliance scores with drill-down by obligation, policy, standard, and control |
| **DevOps/Engineering** | Query compliance status of solutions; integrate automated checks in CI/CD pipelines |
| **Auditors** | Review governance decisions and approval logs; trace obligations to controls with full provenance |
| **Software Engineers** | Check what a specific Standard/Control requires before shipping; ideally check "is my service compliant" — not yet answerable, see [`spikes/query1`](./spikes/query1/example-questions.md)'s H10 |
| **Security Engineers** | Find coverage gaps below the Policy level (governed capabilities with no working Control yet); reason about blast radius if a specific control fails |
| **Engineering Managers** | Get whole-team/whole-org posture summaries and prioritized punch lists — open-ended synthesis questions, not single-entity lookups |

---

## Target Outcomes

- **For EU-based companies**: Turn regulatory burden into competitive advantage through compliant-by-design operations by reducing manual compliance effort from days/weeks to minutes/hours
- **For regulators**: Enable trusted, auditable demonstration of compliance (the "moat")

---

---

## Key Differentiators

| Capability | Industry Average | Policy System |
|------------|------------------|---------------|
| Compliance process | Manual spreadsheets, ad-hoc processes | Captured as Executable governance workflows in system |
| Obligation extraction | Manual review (6-10 days avg) | LLM-driven automatic extraction into structured model |
| Mapping to controls | Spreadsheet/keyword search | Semantic graph matching obligations ↔ policies ↔ controls |
| Regulatory updates | Full document re-ingestion | Delta-only updates - change what changed, not everything |
| Automated checks | CI/CD with hardcoded rules | Natural language queries + semantic graph for dynamic compliance checking |
