# © 2026 Cartman ApS. All rights reserved.
"""GraphRAG-SDK GraphSchema mirroring docs/artifacts/ps-domain-concepts.md.

Node/edge labels and cardinalities are copied from that document as of the
version read for this spike. This module does not attempt to replicate
ps-domain-concepts.md's identity conventions (e.g. Capability's
cap_{slug}_{hash}, derived from name alone to force cross-regulation
convergence) — GraphRAG-SDK generates its own deterministic "name__type"
entity IDs and has no extension point for custom ID logic (verified against
the SDK's graph-schema.md before building this). That gap is a deliberate,
documented comparison point (see README.md "Convergence approach"), not
something this schema module works around.
"""

from graphrag_sdk import Entity, Ontology, Relation

SCHEMA = Ontology(
    entities=[
        Entity(
            label="Regulation",
            description=(
                "A regulation source (external EU legislation/standard/national law, "
                "or an internal Business Regulation), identified by official identifier, "
                "title, effective date, version, and status. Root of the domain model. "
                "IMPORTANT: only create a Regulation entity for the specific regulation "
                "this document IS -- not for other regulations, directives, or acts it "
                "merely cites, references, or amends in passing (e.g. a footnote citing "
                "'Regulation (EU) 2019/881' is NOT a reason to create a Regulation node "
                "for it). A Regulation only ever relates directly to a Role (via DEFINES), "
                "a Requirement (via EXPRESSES), or another Regulation (via SUPERSEDED_BY, "
                "and only for a true prior/later version of itself) -- never directly to "
                "a Capability, PracticeArea, Obligation, Policy, Standard, or Control. If "
                "the text seems to connect a Regulation straight to one of those, find the "
                "intermediate Requirement/Obligation entity instead of skipping to it."
            ),
        ),
        Entity(
            label="Role",
            description=(
                "An actor type defined by a regulation that carries duties, e.g. "
                "'Manufacturer' (CRA), 'Data Controller' (GDPR), 'Operator of Essential "
                "Services' (NIS2). Answers 'who must do what' under a given regulation. "
                "A Role only ever relates directly to an Obligation (via HAS) -- never "
                "directly to another Role, a Requirement, or a Capability. If the text "
                "seems to connect two Roles, or a Role straight to a Requirement or "
                "Capability, find the intermediate Obligation entity instead."
            ),
        ),
        Entity(
            label="Requirement",
            description=(
                "A condition expressed by a regulation specifying what must, must not, "
                "or should be true, independent of who is responsible. Anchored to a "
                "specific article/section of exactly one regulation."
            ),
        ),
        Entity(
            label="Obligation",
            description=(
                "A canonical, reusable duty assigned to exactly one Role, e.g. 'Conduct "
                "Cybersecurity Risk Assessment' or 'Report Security Incidents'. Meant to "
                "be shared across Requirements from different regulations that express "
                "the same underlying duty."
            ),
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
                "Example: (CRA)-[DEFINES]->(Manufacturer). Do NOT use DEFINES between two "
                "Regulations (see SUPERSEDED_BY) or between a Regulation and anything other "
                "than a Role."
            ),
            patterns=[("Regulation", "Role")],
        ),
        Relation(
            label="EXPRESSES",
            description=(
                "The Regulation is the source, the Requirement stated at a specific "
                "article/section is the target. Example: (CRA)-[EXPRESSES]->(Requirement: "
                "'shall ensure...', Art. 13.1). Do NOT link a Regulation directly to a "
                "Capability, PracticeArea, or Obligation with this relation -- those are only "
                "reached via the chain Requirement->Obligation->Capability, never directly "
                "from the Regulation."
            ),
            patterns=[("Regulation", "Requirement")],
        ),
        Relation(
            label="SUPERSEDED_BY",
            description=(
                "An earlier version of a Regulation is superseded by a later version of the "
                "SAME regulation (e.g. a repealed act superseded by its replacement). Do NOT "
                "use this for a Regulation merely mentioning, citing, or cross-referencing a "
                "different, unrelated regulation -- that is not a supersession and should not "
                "be extracted as a relationship of any type."
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
                "The Requirement is the source, the canonical Obligation that fulfills it is "
                "the target. Example: (Requirement: 'shall conduct a risk "
                "assessment')-[SATISFIED_BY]->(Conduct Cybersecurity Risk Assessment). Do NOT "
                "use this between two Requirements, or with a Capability on either end."
            ),
            patterns=[("Requirement", "Obligation")],
        ),
        Relation(
            label="REQUIRES",
            description=(
                "The Obligation is the source, the Capability needed to fulfill it is the "
                "target. Example: (Conduct Cybersecurity Risk Assessment)-[REQUIRES]->"
                "(Cybersecurity Risk Assessment Process). Do NOT use REQUIRES directly from a "
                "Regulation or Requirement -- only from an Obligation."
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
                "Example: (Secure Development Lifecycle)-[OWNS]->(Secure SDLC Policy). Do NOT "
                "reverse this -- a Policy does not own a PracticeArea."
            ),
            patterns=[("PracticeArea", "Policy")],
        ),
        Relation(
            label="MITIGATED_BY",
            description=(
                "The RiskPath is the source, the Capability that mitigates it is the target -- "
                "even though in prose this reads backwards ('the Capability mitigates the "
                "risk'). Example: (Secure Build and Release risk path)-[MITIGATED_BY]->"
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
                "path)-[VERIFIED_BY]->(Pre-Release Threat Model Enforcement Check). Do NOT "
                "reverse this to (Control)-[VERIFIED_BY]->(RiskPath)."
            ),
            patterns=[("RiskPath", "Control")],
        ),
    ],
)
