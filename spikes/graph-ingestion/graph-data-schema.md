# Graph Data Schema

This document defines the data structures used to store Policy System concepts in the FalkorDB (Redis Graph) database.

---

## Overview

The graph database stores five core domain concepts as nodes, connected by typed edges representing relationships between them:

```
Regulation -(contains)-> Obligation -(fulfills)-> Business Policy -(implemented_by)-> Standard -(validates)-> Control
```

Each node has:
- One or more labels (e.g., `:Regulation`, `:Obligation`)
- Properties describing the entity
- A unique internal Redis ID (ephemeral)

Edges have a single type (e.g., `CONTAINS`) and may include properties.

---

## Node Types

### 1. Regulation

Represents an external regulation source (EU legislation, international standard).

#### Labels
- `Regulation`

#### Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Official identifier (e.g., "GDPR", "CRA", "NIS2") |
| `title` | string | Yes | Human-readable title |
| `jurisdiction` | string | No | Geographic scope (e.g., "EU", "Global") |
| `effective_date` | datestring | No | When regulation becomes effective |
| `version` | string | No | Regulation version identifier |
| `status` | string | No | One of: "active", "superseded", "vacated" |

#### Example Node
```cypher
(:Regulation {
    id: "GDPR",
    title: "General Data Protection Regulation",
    jurisdiction: "EU",
    effective_date: "2018-05-25",
    version: "2.0",
    status: "active"
})
```

---

### 2. Obligation

Represents an extractable requirement from a regulation.

#### Labels
- `Obligation`

#### Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique identifier for this obligation |
| `type` | string | Yes | One of: "requirement", "prohibition", "recommendation" |
| `text` | string | Yes | Full text of the obligation |
| `confidence` | float | Yes | LLM confidence score (0.0-1.0, typically ≥0.90) |
| `source_ref` | string | Yes | Location in source regulation (e.g., "Article 32", "Section 5.1") |

#### Example Node
```cypher
(:Obligation {
    id: "obl_gdpr_32_1",
    type: "requirement",
    text: "The controller shall implement technical measures to ensure appropriate levels of security.",
    confidence: 0.95,
    source_ref: "Article 32(1)"
})
```

---

### 3. Business Policy

Represents organizational commitments to address regulatory obligations.

#### Labels
- `Policy`
- `BusinessPolicy`

#### Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique policy identifier |
| `title` | string | Yes | Human-readable title |
| `description` | string | No | Detailed description of the policy |
| `owner_id` | string | No | ID of person/department responsible |
| `status` | string | Yes | One of: "draft", "approved", "deprecated" |
| `version` | string | No | Policy version number |

#### Example Node
```cypher
(:Policy {
    id: "pol_encryption_01",
    title: "Data Encryption Standard",
    description: "All personal data at rest must be encrypted using AES-256",
    owner_id: "security-team",
    status: "approved",
    version: "1.0"
})
```

**Note**: Node can have multiple labels (`Policy` for graph operations, `BusinessPolicy` to distinguish from other policy types).

---

### 4. Standard

Represents implementation guidelines and procedures.

#### Labels
- `Standard`

#### Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique standard identifier |
| `title` | string | Yes | Human-readable title |
| `description` | string | No | Detailed implementation procedure |
| `implementation_status` | string | Yes | One of: "draft", "implemented", "reviewed", "deprecated" |
| `version` | string | No | Standard version number |
| `valid_from` | datestring | No | When this standard becomes effective |
| `valid_until` | datestring | No | When this standard expires |

#### Example Node
```cypher
(:Standard {
    id: "std_aes256_01",
    title: "AES-256 Encryption Implementation Standard",
    description: "Implement AES-256 for all personal data at rest using OpenSSL 3.0+",
    implementation_status: "implemented",
    version: "1.0"
})
```

---

### 5. Control

Represents technical verification mechanisms.

#### Labels
- `Control`

#### Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique control identifier |
| `type` | string | Yes | One of: "automated", "manual" |
| `title` | string | Yes | Human-readable title |
| `description` | string | No | Control procedure description |
| `implementation_status` | string | Yes | One of: "planned", "implemented", "reviewed", "deprecated" |
| `execution_frequency` | string | No | e.g., "daily", "weekly", "monthly", "on_deploy" |
| `last_test_date` | datestring | No | When control was last executed |
| `next_review_date` | datestring | Yes | When control should next be reviewed |

#### Example Node
```cypher
(:Control {
    id: "ctrl_autoscan_01",
    type: "automated",
    title: "Automated Data Discovery Scan",
    description: "Daily scan of all data stores to identify personal data",
    implementation_status: "implemented",
    execution_frequency: "daily",
    last_test_date: "2026-07-31",
    next_review_date: "2026-08-31"
})
```

---

## Edge Types

### `CONTAINS`
**Direction**: Regulation → Obligation  
**Meaning**: The regulation contains this obligation

```cypher
(:Regulation)-[:CONTAINS]->(:Obligation)
```

**Properties**: None (edge may be annotated with source article/section)

---

### `FULFILLS`
**Direction**: Obligation → Business Policy  
**Meaning**: The policy addresses this regulatory obligation

```cypher
(:Obligation)-[:FULFILLS]->(:Policy)
```

**Properties**: None

**Note**: Inverse relationship exists on the Policy side (`fulfills`).

---

### `IMPLEMENTED_BY`
**Direction**: Business Policy → Standard  
**Meaning**: The standard implements this policy requirement

```cypher
(:Policy)-[:IMPLEMENTED_BY]->(:Standard)
```

**Properties**: None

---

### `VALIDATES`
**Direction**: Standard → Control  
**Meaning**: The control verifies adherence to this standard

```cypher
(:Standard)-[:VALIDATES]->(:Control)
```

**Properties**: None

---

## Complete Relationship Graph

```
┌──────────────┐      CONTAINS       ┌─────────────┐
│ Regulation   ├─────────────────────▶│ Obligation  │
└──────────────┘                      └─────────────┘
                                             ▲
                   FULFILLS                  │
                    │                        │
                    ▼                        │
┌──────────────┐                            │
│   Policy     ◀───────────────────────────┤
└──────────────┘                            │
                    │                        │
         IMPLEMENTED_BY                    │
                    ▼                        │
┌──────────────┐                            │
│  Standard    ├───────────────────────────┤
└──────────────┘                           │
                     VALIDATES             │
                    │                      │
                    ▼                      │
┌──────────────┐                            │
│   Control    ◀───────────────────────────┘
└──────────────┘
```

---

## Graph Schema to Domain Model Mapping

| Domain Concept | Graph Label(s) | Key Properties |
|----------------|----------------|----------------|
| Regulation | `Regulation` | id, title, jurisdiction, version, status |
| Obligation | `Obligation` | id, type, text, confidence, source_ref |
| Business Policy | `Policy`, `BusinessPolicy` | id, title, owner_id, status, version |
| Standard | `Standard` | id, title, implementation_status, version |
| Control | `Control` | id, type, implementation_status, execution_frequency |

---

## Query Examples

### Find all obligations in a regulation
```cypher
MATCH (r:Regulation {id: "GDPR"})-[:CONTAINS]->(o:Obligation)
RETURN o.id, o.type, o.text
```

### Find which policies fulfill a regulatory obligation
```cypher
MATCH (o:Obligation {id: "gdpr_art_32"})->[:FULFILLS]->(p:Policy)
RETURN p.id, p.title, p.owner_id
```

### Show full compliance chain for an obligation
```cypher
MATCH (r:Regulation)-[:CONTAINS]->(o:Obligation {id: "gdpr_art_32"})
      -[:FULFILLS]->(p:Policy)-[:IMPLEMENTED_BY]->(s:Standard)
      -[:VALIDATES]->(c:Control)
RETURN r.title AS regulation, o.text AS obligation,
       p.title AS policy, s.title AS standard, c.title AS control
```

### Find unfulfilled obligations (no policies mapped)
```cypher
MATCH (o:Obligation)
WHERE NOT (o)-[:FULFILLS]->(:Policy)
RETURN o.id, o.source_ref
```

---

## Idempotency Considerations

**Current Implementation**: Uses `CREATE` which fails on duplicate nodes.

**Production Recommendation**: Use `MERGE` with unique constraints:

```cypher
// Instead of CREATE for idempotence
MERGE (r:Regulation {id: $reg_id})
ON CREATE SET r += $properties
ON MATCH SET r.last_seen = timestamp()
RETURN id(r) AS node_id
```

Define unique constraints on business IDs to ensure idempotent ingestion:

```cypher
CREATE CONSTRAINT FOR (r:Regulation) REQUIRE r.id IS UNIQUE
CREATE CONSTRAINT FOR (o:Obligation) REQUIRE o.id IS UNIQUE
CREATE CONSTRAINT FOR (p:Policy) REQUIRE p.id IS UNIQUE
CREATE CONSTRAINT FOR (s:Standard) REQUIRE s.id IS UNIQUE
CREATE CONSTRAINT FOR (c:Control) REQUIRE c.id IS UNIQUE
```

---

*End of Document*
