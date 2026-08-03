# Data Model Specification

This document defines the data model mapping between Domain Concepts (business layer) and their graph database representation.

---

## Overview

The Policy System uses a two-layer approach:

1. **Domain Layer** - Business concepts defined in `policy-system-domain-concepts.md`
2. **Storage Layer** - Graph database schema in `graph-data-schema.md`

This document specifies the *mapping* between these layers, including serialization format, ID strategy, and validation rules.

---

## Mapping Matrix

| Domain Concept | Graph Label(s) | Storage Format | Identity Strategy |
|----------------|----------------|----------------|-------------------|
| Regulation | `Regulation` | Node with properties | Business ID (official identifier + version) |
| Obligation | `Obligation` | Node with properties | Generated ID (UUID + source_ref prefix) |
| Business Policy | `Policy`, `BusinessPolicy` | Node with properties | Business ID (short slug) |
| Standard | `Standard` | Node with properties | Business ID (derived from title) |
| Control | `Control` | Node with properties | Business ID (derived from type + purpose) |

---

## Concrete Field Mappings

### 1. Regulation

#### Domain Concept Properties
- Official identifier (e.g., "Regulation (EU) 2024/2847")
- Title: Human-readable name
- Jurisdiction: Geographic scope
- Effective date: When regulation becomes active
- Version number: For change tracking
- Status: active/superseded/vacated

#### Graph Storage Mapping

| Domain Property | Graph Field | Type | Optional |
|-----------------|-------------|------|----------|
| official_identifier | `id` | string | **No** |
| title | `title` | string | **No** |
| jurisdiction | `jurisdiction` | string | Yes |
| effective_date | `effective_date` | datestring (ISO 8601) | Yes |
| version | `version` | string | Yes |
| status | `status` | enum: "active"\|"superseded"\|"vacated" | Yes |

#### Example (Domain → Graph)
```python
# Domain object
regulation = {
    'official_identifier': 'Regulation (EU) 2024/2847',
    'title': 'Cyber Resilience Act',
    'jurisdiction': 'EU',
    'effective_date': datetime(2025, 6, 1),
    'version': '1.0',
    'status': 'active'
}

# Graph node properties
{
    'id': 'CRA-1.0',  # Derived from identifier: "CRA" + version
    'title': 'Cyber Resilience Act',
    'jurisdiction': 'EU',
    'effective_date': '2025-06-01',
    'version': '1.0',
    'status': 'active'
}
```

---

### 2. Obligation

#### Domain Concept Properties
- Unique identifier
- Regulatory source reference (article, section)
- Type: requirement/prohibition/recommendation
- Text: Full obligation statement
- Confidence score (LLM extraction quality)

#### Graph Storage Mapping

| Domain Property | Graph Field | Type | Optional |
|-----------------|-------------|------|----------|
| unique_identifier | `id` | string | **No** |
| source_reference | `source_ref` | string | **No** |
| type | `type` | enum: "requirement"\|"prohibition"\|"recommendation" | **No** |
| text | `text` | string (max 10KB) | **No** |
| confidence_score | `confidence` | float (0.0-1.0) | **No** |

#### ID Generation Strategy

```python
def generate_obligation_id(source_ref: str, text_hash: str) -> str:
    """Generate stable obligation ID from source reference and content hash."""
    # Extract article/section number as prefix
    article_num = extract_article_number(source_ref)  # e.g., "32" from "Article 32(1)"
    return f"obl_{article_num}_{text_hash[:8]}"
```

#### Example (Domain → Graph)
```python
# Domain object (from LLM extraction)
obligation = {
    'source_reference': 'Article 32(1)',
    'type': 'requirement',
    'text': 'The controller shall implement technical measures...',
    'confidence': 0.95,
    'unique_identifier': None  # Generated after extraction
}

# Graph node properties (after ID generation)
{
    'id': 'obl_32_a8f3b1c2',
    'source_ref': 'Article 32(1)',
    'type': 'requirement',
    'text': 'The controller shall implement technical and organizational measures...',
    'confidence': 0.95
}
```

---

### 3. Business Policy

#### Domain Concept Properties
- Title: Human-readable name
- Description: Detailed explanation
- Owner ID: Person/department responsible
- Status: draft/approved/deprecated
- Version: Incremental versioning
- References to fulfilled obligations

#### Graph Storage Mapping

| Domain Property | Graph Field | Type | Optional |
|-----------------|-------------|------|----------|
| title | `title` | string | **No** |
| description | `description` | string (max 20KB) | Yes |
| owner_id | `owner_id` | string (user/team ID) | Yes |
| status | `status` | enum: "draft"\|"approved"\|"deprecated" | **No** |
| version | `version` | string | Yes |

#### ID Generation Strategy

```python
def generate_policy_id(title: str, owner_id: str = None) -> str:
    """Generate policy ID from title and optionally owner."""
    slugify = lambda s: re.sub(r'[^a-z0-9]', '_', s.lower().strip()[:32])
    
    if owner_id:
        return f"pol_{slugify(title)}_{owner_id}"
    else:
        return f"pol_{slugify(title)}"
```

#### Example
```python
{
    'id': 'pol_data_encryption_security',
    'title': 'Data Encryption Standard',
    'description': 'All personal data at rest must be encrypted using AES-256...',
    'owner_id': 'security-team',
    'status': 'approved',
    'version': '1.0'
}
```

---

### 4. Standard

#### Domain Concept Properties
- Title: Implementation procedure name
- Description: Detailed steps
- Implementation status: draft/implemented/reviewed/deprecated
- Version: Incremental versioning
- Validity period (optional)

#### Graph Storage Mapping

| Domain Property | Graph Field | Type | Optional |
|-----------------|-------------|------|----------|
| title | `title` | string | **No** |
| description | `description` | string (max 20KB) | Yes |
| implementation_status | `implementation_status` | enum: "draft"\|"implemented"\|"reviewed"\|"deprecated" | **No** |
| version | `version` | string | Yes |
| valid_from | `valid_from` | datestring | Yes |
| valid_until | `valid_until` | datestring | Yes |

#### Example
```python
{
    'id': 'std_aes256_openssl',
    'title': 'AES-256 Implementation Standard',
    'description': '1. Install OpenSSL 3.0+\n2. Use EVP_EncryptInit_ex...\n...',
    'implementation_status': 'implemented',
    'version': '1.0'
}
```

---

### 5. Control

#### Domain Concept Properties
- Type: automated/manual
- Title: Control name
- Description: Procedure details
- Implementation status: planned/implemented/reviewed/deprecated
- Execution frequency: daily/weekly/monthly/on_deploy/etc.
- Last test date, next review date (optional)

#### Graph Storage Mapping

| Domain Property | Graph Field | Type | Optional |
|-----------------|-------------|------|----------|
| type | `type` | enum: "automated"\|"manual" | **No** |
| title | `title` | string | **No** |
| description | `description` | string (max 10KB) | Yes |
| implementation_status | `implementation_status` | enum: "planned"\|"implemented"\|"reviewed"\|"deprecated" | **No** |
| execution_frequency | `execution_frequency` | string (e.g., "daily", "on_deploy") | Yes |
| last_test_date | `last_test_date` | datestring | Yes |
| next_review_date | `next_review_date` | datestring | Yes |

#### Example
```python
{
    'id': 'ctrl_autoscan_daily',
    'type': 'automated',
    'title': 'Automated Data Discovery Scan',
    'description': 'Daily scan of all data stores to identify personal data using...',
    'implementation_status': 'implemented',
    'execution_frequency': 'daily'
}
```

---

## Relationship Edge Types

| Domain Relationship | Graph Edge Type | Direction | Properties |
|---------------------|-----------------|-----------|------------|
| Regulation *contains* Obligation | `CONTAINS` | Reg → Obs | None |
| Obligation *fulfills* Policy | `FULFILLS` | Obs → Pol | None |
| Policy *implemented_by* Standard | `IMPLEMENTED_BY` | Pol → Std | None |
| Standard *validates* Control | `VALIDATES` | Std → Ctrl | None |

**Note:** Edges are unidirectional to preserve domain directionality. Inverse traversals use Cypher pattern matching.

---

## Validation Rules

### Required Fields (Non-nullable)
```python
REGULATION_REQUIRED = {'id', 'title'}
OBLIGATION_REQUIRED = {'id', 'source_ref', 'type', 'text', 'confidence'}
POLICY_REQUIRED = {'id', 'title', 'status'}
STANDARD_REQUIRED = {'id', 'title', 'implementation_status'}
CONTROL_REQUIRED = {'id', 'title', 'type', 'implementation_status'}
```

### Enum Constraints
```python
OBLIGATION_TYPES = {'requirement', 'prohibition', 'recommendation'}
POLICY_STATUS = {'draft', 'approved', 'deprecated'}
STANDARD_STATUS = {'draft', 'implemented', 'reviewed', 'deprecated'}
CONTROL_STATUS = {'planned', 'implemented', 'reviewed', 'deprecated'}
CONTROL_TYPE = {'automated', 'manual'}
```

### Data Type Constraints
- `confidence`: 0.0 ≤ float ≤ 1.0
- `text` fields: max 20KB (large enough for full article text)
- `datestring`: ISO 8601 format (YYYY-MM-DD)

---

## Serialization Formats

### JSON (for API/transport)
```json
{
  "id": "CRA-1.0",
  "title": "Cyber Resilience Act",
  "jurisdiction": "EU",
  "effective_date": "2025-06-01",
  "version": "1.0"
}
```

### Cypher (for database)
```cypher
CREATE (r:Regulation {
    id: 'CRA-1.0',
    title: 'Cyber Resilience Act',
    jurisdiction: 'EU',
    effective_date: '2025-06-01',
    version: '1.0'
})
```

---

## Traceability Matrix

| Domain Concept | Domain Doc Section | Graph Schema Doc | Data Model (This Doc) |
|----------------|-------------------|------------------|----------------------|
| Regulation | §4.1 | `graph-data-schema.md` §1 | This doc: 1.Regulation |
| Obligation | §4.2 | `graph-data-schema.md` §2 | This doc: 2.Obligation |
| Business Policy | §4.3 | `graph-data-schema.md` §3 | This doc: 3.Business Policy |
| Standard | §4.4 | `graph-data-schema.md` §4 | This doc: 4.Standard |
| Control | §4.5 | `graph-data-schema.md` §5 | This doc: 5.Control |

---

## ID Generation Strategy (Cross-Cutting)

### Business IDs (Preferred)
- Human-readable, meaningful
- Stable across generations
- Example: "CRA-1.0", "pol_data_encryption_security"

### Generated IDs (When needed)
- UUIDs for temporary identifiers
- Hash-based IDs for content-derived entities (obligations)

**Rule:** Use business IDs when possible; only use generated IDs when:
1. No natural identifier exists (e.g., obligations extracted from text)
2. Temporary placeholder IDs are needed during ingestion

---

*End of Document*
