---
name: container-architecture
description: >-
  Read, create, validate, and update Container Architecture documents. Use when
  working with C4 component-level design, component decomposition, info models,
  action specifications, or when a change request affects container internals.
  Validates INV-005 and INV-011.
  Used by the Solution Architect agent.
metadata:
  author: platform
  version: "1.0.0"
  tags: [sdlc, architecture, container, c4]
  copyright: "© 2026 Cartman ApS. All rights reserved."
---

# Container Architecture

## Purpose

Manage Container Architecture documents that bridge Solution Architecture (C4 Container
level) with implementation details (C4 Component level). Each container in the system
has its own Container Architecture document.

## Inputs

- `system-config.md` — project configuration and artifact paths
- Domain Terms — canonical vocabulary
- Domain Concepts — entities, relationships
- Solution Architecture — containers, domain paths
- User Requirements — use cases

## Outputs

- Container Architecture document (`${SystemName}-${ContainerName}-container-architecture.md`)

## Validation

- **INV-005:** All Components in CA exist in SA Container Breakdown
- **INV-011:** Every CA entity exists in DC

## Template

- **Source:** `assets/container-architecture-template.md`
- **Output naming:** `${SystemName}-${ContainerName}-container-architecture.md`

## Instructions

### Reading Container Architecture

1. Load `system-config.md` to resolve artifact paths
2. Read the Container Architecture document from the services registry
3. Extract components, info model, actions, and constraints

### Creating Container Architecture

**Confidence Check Pattern:** At the end of analysis phases, provide a confidence
level (percentage) with supporting evidence. If confidence is <75%, inform the user
and STOP.

#### Phase 0: Prerequisites

1. Verify all required documents exist: IUD, Domain Terms, Domain Concepts, URS, Solution Architecture
2. If any prerequisite is missing:
   - Name the missing artifact(s) and explain what they provide to Container Architecture:
     - Missing IUD → provides business context and user roles
     - Missing Domain Terms → provides the canonical vocabulary for component and action naming
     - Missing Domain Concepts → provides entities that components implement
     - Missing URS → provides use cases for coverage mapping
     - Missing Solution Architecture → provides the container definition and domain paths this document drills into
   - Recommend the agent mode and prompt to create them:
     - Missing IUD → _"Switch to `product-owner` and ask: 'Create an intended use document'"_
     - Missing Domain Terms → _"Switch to `product-owner` and ask: 'Create domain terms'"_
     - Missing Domain Concepts → _"Switch to `product-owner` and ask: 'Create domain concepts'"_
     - Missing URS → _"Switch to `product-owner` and ask: 'Create user requirements'"_
     - Missing Solution Architecture → _"Switch to `solution-architect` and ask: 'Create a solution architecture'"_
   - STOP — do not proceed with incomplete inputs
3. Check the Status field of each prerequisite artifact. If any are in Draft status (or have no Status field), present a consolidated warning:
   - List all Draft/missing-status artifacts
   - Explain: _"These artifacts have not been approved. Building on unapproved artifacts may produce results that need rework. Continue anyway?"_
   - If the user chooses not to continue, STOP
4. Read Solution Architecture and extract all Container Services
5. Ask user which container to create architecture for

#### Phase 1: Preparation

1. If document does not exist, create from `assets/container-architecture-template.md`
2. If document exists, verify it matches the template structure

#### Phase 2: Identify Components

1. Analyze the Solution Architecture container definition
2. Analyze the codebase for internal component structure
3. For each component, document:
   - Component name (PascalCase)
   - Complete domain path (`system.container.component`)
   - Key responsibilities (implementation-agnostic)
4. Ensure responsibilities are non-overlapping (clear separation of concerns)

#### Phase 3: Domain Concept Mapping

1. Map each Domain Concept to implementing components
2. If a concept spans multiple components, document justification
3. Include implementation notes explaining HOW the concept is realized

#### Phase 4: Define Actions

1. For each component, define actions with:
   - Purpose, Authentication, Authorization
   - Pre/Post-conditions, Side Effects
   - External Dependencies, SLA, Idempotency
   - Error Handling
2. Create action sequence diagrams for critical paths (happy path and error scenarios)

#### Phase 5: Use Case Coverage

1. Map every component to at least one URS use case
2. Verify critical URS use cases are covered by this container

#### Phase 6: Validation

1. Validate INV-005: all components exist in SA Container Breakdown
2. Validate INV-011: every entity in CA exists in Domain Concepts
3. Verify diagrams match component tables (tables are source of truth)
4. Inform the user: _"This artifact is in Draft status. Review the content and set the Status to Approved when satisfied."_

### Impact Assessment

When evaluating a change request's impact on Container Architecture:

1. Identify affected components
2. Evaluate changes to: component responsibilities, domain concept mappings, actions, info model
3. Check for new components needed or existing components to modify
4. Assess sequence diagram changes
5. Validate use case coverage remains complete

### Implementation

When implementing changes from an approved Impact Assessment:

1. Read the IA and identify all INCLUDED changes
2. Update component tables, domain concept mappings, and actions
3. Update sequence diagrams for modified flows
4. Re-validate INV-005 and INV-011
5. Ensure consistency between tables and diagrams

### Rules

- Domain path format: `<system>.<container>.<component>` (lowercase dot notation)
- All components must trace to Domain Concepts and URS requirements
- Component descriptions focus on responsibilities, not implementation
- Component responsibilities must be non-overlapping
- Actions must document all specified attributes (purpose, auth, SLA, etc.)
- Tables are source of truth; diagrams must match
- DO NOT include document versioning — managed via Git
