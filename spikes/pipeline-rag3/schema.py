# © 2026 Cartman ApS. All rights reserved.
"""GraphRAG-SDK Ontology for pipeline-rag3 (native CRA-graph). Source ontology shared with sibling spikes (unchanged behaviour); header de-scoped to pr3: native output only, mapping to policy_system shape is pipeline-rag4..

Derived from spikes/pipeline-rag/schema.py with two deliberate changes:
1. Anti-shortcut additions to Regulation/Role entity descriptions are REMOVED
   — they were measured to make extraction worse (38→49 pruned, per
   pipeline-rag/LEARNINGS.md). Direction-explicit relation descriptions
   are kept — they were measured to help (61→38 pruned).
2. Attribute extensions on Obligation/Capability for domain properties that
   the LLM should produce (confidence, obligation_type, type) that the
   transformer can then pass through to the cra.json-format output.

This module does NOT attempt to replicate ps-domain-concepts.md's ID
conventions (cap_{slug}_{hash}, cap convergence by name alone, etc.).
That is transform.py's job, not the LLM's.
"""

from graphrag_sdk import Attribute, Entity, Ontology, Relation


SCHEMA = Ontology(
    entities=[
        Entity(
            label="Regulation",
            description=(
                "A regulation source (external EU legislation/standard/national law, "
                "or an internal Business Regulation), identified by official identifier, "
                "title, effective date, version, and status. Root of the domain model. "
                "Extract exactly one Regulation entity per ingested document — the "
                "regulation that document IS, or an internal regulation it defines. "
                "Do NOT create a Regulation entity for other regulations, directives, "
                "or acts that the document merely cites, references, or amends in passing "
                "(e.g. a footnote citing 'Regulation (EU) 2019/881' is not a reason to "
                "create a Regulation node for it)."
            ),
        ),
        Entity(
            label="Role",
            description=(
                "An actor type defined by a regulation that carries duties, e.g. "
                "'Manufacturer' (CRA), 'Data Controller' (GDPR), 'Operator of Essential "
                "Services' (NIS2). Answers 'who must do what' under a given regulation. "
                "A Role only ever relates directly to an Obligation (via HAS), and is "
                "itself related to a Regulation (via DEFINES). A Role never relates "
                "directly to a Requirement, Capability, or another Role."
            ),
        ),
        Entity(
            label="Requirement",
            description=(
                "A condition expressed by a regulation specifying what must, must not, "
                "or should be true, independent of who is responsible. Anchored to a "
                "specific article/section of exactly one regulation. In text, a "
                "Requirement carries a source_ref naming the article or section it "
                "comes from (e.g. 'Art. 13.1'). The source_ref also determines "
                "the Requirement's canonical id: {reg_id}_req_{article_ref}."
            ),
        ),
        Entity(
            label="Obligation",
            description=(
                "A canonical, reusable duty assigned to exactly one Role, e.g. 'Conduct "
                "Cybersecurity Risk Assessment' or 'Report Security Incidents'. Meant to "
                "be shared across Requirements from different regulations that express "
                "the same underlying duty. A single Requirement is SATISFIED_BY exactly "
                "one Obligation (in this domain model)."
            ),
            properties=[
                Attribute(
                    name="confidence",
                    type="FLOAT",
                    description=(
                        "Extraction confidence in [0.0, 1.0]. Reflect how clearly the "
                        "source text states this duty as a distinct obligation. "
                        "Well-defined duties: >= 0.9. Ambiguous or permissive duties: "
                        "0.7-0.89."
                    ),
                ),
                Attribute(
                    name="obligation_type",
                    type="STRING",
                    description=(
                        "'technical' for duties that involve building or operating a "
                        "system, 'organizational' for governance, process, or procedural "
                        "duties. Choose one; do not produce both."
                    ),
                ),
            ],
        ),
        Entity(
            label="PracticeArea",
            description=(
                "A stable engineering taxonomy grouping related Capabilities and "
                "governing Policy ownership, e.g. 'Secure Development Lifecycle'. "
                "Organizational classification, not compliance provenance."
            ),
        ),
        Entity(
            label="RiskPath",
            description=(
                "A cross-cutting risk lens used to reason about completeness and gaps, "
                "e.g. 'Secure Build and Release' or 'Incident and Recovery Readiness'."
            ),
        ),
        Entity(
            label="Capability",
            description=(
                "A technical or organizational capacity that must exist to fulfill "
                "Obligations, e.g. 'Data Encryption', 'Security Logging'. The point of "
                "convergence where semantically-equivalent duties from different "
                "regulations should collapse onto the same capacity."
            ),
            properties=[
                Attribute(
                    # DEFECT-1 FIX: was 'type', which collided with the SDK node
                    # discriminator `n.type` (value 'technical' overwrote the label).
                    # Renamed to 'capability_type' at the native layer; transform.py
                    # maps native `capability_type` -> domain `type` (cra.json shape).
                    name="capability_type",
                    type="STRING",
                    description=(
                        "'technical' for a capacity that involves building or operating a "
                        "system or mechanism, 'organizational' for a capacity that is a "
                        "governance, process, or documentation function. Choose one."
                    ),
                ),
            ],
        ),
        Entity(
            label="Policy",
            description=(
                "An organizational commitment governing how one or more Capabilities "
                "must be achieved. Carries an owner, a draft/approved/deprecated status, "
                "and a review cycle."
            ),
        ),
        Entity(
            label="Standard",
            description=(
                "Implementation guidance for how exactly one Policy is actually to be "
                "achieved: procedures, technical specifications, testing expectations."
            ),
        ),
        Entity(
            label="Control",
            description=(
                "A concrete, testable verification mechanism (automated or manual) "
                "confirming that exactly one Standard's procedure is being followed. "
                "Carries execution frequency, test dates, and evidence references."
            ),
        ),
    ],
    relations=[
        Relation(
            label="DEFINES",
            description=(
                "The Regulation is the source, the Role it assigns duties to is the target. "
                "Example: (CRA)-[DEFINES]->(Manufacturer, source_ref='Art. 3'). "
                "The source_ref on this edge names the article in which the Role is "
                "defined. Do NOT use DEFINES between two Regulations or between a "
                "Regulation and anything other than a Role."
            ),
            patterns=[("Regulation", "Role")],
            properties=[
                Attribute(name="source_ref", type="STRING",
                    description="Article or section where the Role is defined, e.g. 'Art. 3'."),
            ],
        ),
        Relation(
            label="EXPRESSES",
            description=(
                "The Regulation is the source, the Requirement stated at a specific "
                "article/section is the target. Example: (CRA)-[EXPRESSES]->"
                "(Requirement: 'shall ensure...', source_ref='Art. 13.1'). The source_ref "
                "on this edge names the article/section and determines the Requirement's "
                "canonical id. Do NOT link a Regulation directly to a Capability, "
                "PracticeArea, or Obligation — those are only reached via the chain "
                "Requirement->Obligation->Capability, never directly from the Regulation."
            ),
            patterns=[("Regulation", "Requirement")],
            properties=[
                Attribute(name="source_ref", type="STRING",
                    description="Article or section where the Requirement is stated, "
                                 "e.g. 'Art. 13.1'."),
            ],
        ),
        Relation(
            label="SUPERSEDED_BY",
            description=(
                "An earlier version of a Regulation is superseded by a later version of "
                "the SAME regulation (e.g. a repealed act superseded by its replacement). "
                "Do NOT use this for a Regulation merely mentioning, citing, or "
                "cross-referencing a different, unrelated regulation."
            ),
            patterns=[("Regulation", "Regulation")],
        ),
        Relation(
            label="HAS",
            description=(
                "The Role is the source, the canonical Obligation assigned to it is the "
                "target. Example: (Manufacturer)-[HAS]->(Conduct Cybersecurity Risk "
                "Assessment). Do NOT use HAS to link a Role directly to a Requirement, "
                "Capability, or another Role."
            ),
            patterns=[("Role", "Obligation")],
        ),
        Relation(
            label="SATISFIED_BY",
            description=(
                "The Requirement is the source, the canonical Obligation that fulfills it "
                "is the target. Example: (Requirement: 'shall conduct a risk "
                "assessment')-[SATISFIED_BY]->(Conduct Cybersecurity Risk Assessment). "
                "Do NOT use this between two Requirements, or with a Capability on either "
                "end."
            ),
            patterns=[("Requirement", "Obligation")],
        ),
        Relation(
            label="REQUIRES",
            description=(
                "The Obligation is the source, the Capability needed to fulfill it is the "
                "target. Example: (Conduct Cybersecurity Risk Assessment)-[REQUIRES]->"
                "(Cybersecurity Risk Assessment Process). Do NOT use REQUIRES directly "
                "from a Regulation or Requirement — only from an Obligation."
            ),
            patterns=[("Obligation", "Capability")],
        ),
        Relation(
            label="COVERS",
            description=(
                "The PracticeArea is the source, the Capability it classifies is the target. "
                "Example: (Secure Development Lifecycle)-[COVERS]->(Data Encryption)."
            ),
            patterns=[("PracticeArea", "Capability")],
        ),
        Relation(
            label="OWNS",
            description=(
                "The PracticeArea is the source, the Policy it governs is the target. "
                "Example: (Secure Development Lifecycle)-[OWNS]->(Secure SDLC Policy). "
                "Do NOT reverse this — a Policy does not own a PracticeArea."
            ),
            patterns=[("PracticeArea", "Policy")],
        ),
        Relation(
            label="MITIGATED_BY",
            description=(
                "The RiskPath is the source, the Capability that mitigates it is the target "
                "-- even though in prose this reads backwards ('the Capability mitigates "
                "the risk'). Example: (Secure Build and Release risk path)-[MITIGATED_BY]->"
                "(Dependency Provenance and License Review). Do NOT reverse this to "
                "(Capability)-[MITIGATED_BY]->(RiskPath)."
            ),
            patterns=[("RiskPath", "Capability")],
        ),
        Relation(
            label="GOVERNED_BY",
            description=(
                "The Capability is the source, the Policy governing it is the target. "
                "Example: (Data Encryption)-[GOVERNED_BY]->(Data Protection Policy)."
            ),
            patterns=[("Capability", "Policy")],
        ),
        Relation(
            label="SUPPORTED_BY",
            description=(
                "The Policy is the source, the Standard defining how it's implemented is the "
                "target. Example: (Secure SDLC Policy)-[SUPPORTED_BY]->(Threat Modeling and "
                "Secure Coding Standard)."
            ),
            patterns=[("Policy", "Standard")],
        ),
        Relation(
            label="IMPLEMENTED_BY",
            description=(
                "The Standard is the source, the Control that verifies it is the target. "
                "Example: (Threat Modeling and Secure Coding Standard)-[IMPLEMENTED_BY]->"
                "(Pre-Release Threat Model Enforcement Check)."
            ),
            patterns=[("Standard", "Control")],
        ),
        Relation(
            label="VERIFIED_BY",
            description=(
                "The RiskPath is the source, the Control providing verification evidence is "
                "the target -- even though in prose this reads backwards ('the Control "
                "verifies the risk'). Example: (Secure Build and Release risk "
                "path)-[VERIFIED_BY]->(Pre-Release Threat Model Enforcement Check). "
                "Do NOT reverse this to (Control)-[VERIFIED_BY]->(RiskPath)."
            ),
            patterns=[("RiskPath", "Control")],
        ),
    ],
)
