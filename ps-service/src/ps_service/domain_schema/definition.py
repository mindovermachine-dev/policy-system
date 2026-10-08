"""The code-defined PS domain schema: the only place schema facts are typed in.

Facts are transcribed from `docs/artifacts/ps-domain-concepts.md`; notes are one-line
summaries (the long prose stays in the document).
"""

from __future__ import annotations

from ps_service.domain_schema.model import (
    Cardinality,
    DateType,
    Edge,
    EdgeProperty,
    EnumType,
    FloatRangeType,
    Node,
    Presence,
    Property,
    Schema,
    StringType,
)

_STRING = StringType()
_CONFIDENCE = FloatRangeType(minimum=0.0, maximum=1.0)
_REQUIRED = Presence.REQUIRED
_OPTIONAL = Presence.OPTIONAL
_ACTIVE_DEPRECATED = EnumType(("active", "deprecated"))
_GOVERNANCE_STATUSES = ("draft", "proposed", "approved", "deprecated")
_SHORT_SUMMARY_NOTE = (
    "Short human-readable summary — not a catch-all; the structured fields "
    "below hold the substantive content."
)
_GOVERNANCE_STATUS_NOTE = (
    "Governance status, independent of implementation_status; cascades from "
    "the parent Policy (see Policy Lifecycle, issue #134)."
)

_REGULATORY_INSTRUMENT = Node(
    label="RegulatoryInstrument",
    properties=(
        Property("id", _STRING, _REQUIRED, "Same value as Identity above"),
        Property("title", _STRING, _REQUIRED),
        Property(
            "source_type",
            EnumType(("external", "internal")),
            _REQUIRED,
            (
                "`external` = EU legislation, international standard, or national law. "
                "`internal` = organizationally-authored Business Regulation (e.g. "
                "Engineering Practices standard)."
            ),
        ),
        Property(
            "instrument_type",
            EnumType(("regulation", "directive", "national_transposition")),
            Presence.CONDITIONAL,
            "Required for source_type external; absent for internal. Shapes the identity.",
        ),
        Property(
            "jurisdiction",
            _STRING,
            _OPTIONAL,
            "Needed for external sources; for national_transposition an ISO 3166-1 alpha-2 code.",
        ),
        Property("effective_date", DateType(), _REQUIRED),
        Property("version", _STRING, _REQUIRED),
        Property("status", EnumType(("active", "superseded", "vacated")), _REQUIRED),
    ),
)

_ROLE = Node(
    label="Role",
    properties=(
        Property("name", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL),
        Property(
            "confidence",
            _CONFIDENCE,
            _REQUIRED,
            "Extractor's certainty that this is a genuine duty-bearing actor category.",
        ),
    ),
)

_REQUIREMENT = Node(
    label="Requirement",
    properties=(
        Property("text", _STRING, _REQUIRED),
        Property("type", EnumType(("requirement", "prohibition", "recommendation")), _REQUIRED),
        Property("status", EnumType(("active", "deprecated")), _OPTIONAL),
        Property(
            "confidence",
            _CONFIDENCE,
            _REQUIRED,
            "Extractor's certainty that this paragraph genuinely states an operative requirement.",
        ),
    ),
)

_OBLIGATION = Node(
    label="Obligation",
    properties=(
        Property(
            "text",
            _STRING,
            _REQUIRED,
            'The duty statement, e.g. "Conduct Cybersecurity Risk Assessment"',
        ),
        Property(
            "confidence",
            _CONFIDENCE,
            _REQUIRED,
            "Extractor's certainty in minting or matching this Obligation; always recorded.",
        ),
    ),
)

_PRACTICE_AREA = Node(
    label="PracticeArea",
    properties=(
        Property("name", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL),
        Property("status", _ACTIVE_DEPRECATED, _REQUIRED),
        Property("version", _STRING, _OPTIONAL),
        Property(
            "owner_id", _STRING, _OPTIONAL, "Optional organizational owner for this taxonomy area."
        ),
    ),
)

_RISK_PATH = Node(
    label="RiskPath",
    properties=(
        Property("name", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL),
        Property("status", _ACTIVE_DEPRECATED, _REQUIRED),
        Property(
            "risk_type",
            EnumType(
                ("security", "reliability", "privacy", "compliance", "safety", "supply_chain")
            ),
            _OPTIONAL,
            "Optional categorization for reporting slices.",
        ),
        Property("version", _STRING, _OPTIONAL),
    ),
)

_CAPABILITY = Node(
    label="Capability",
    properties=(
        Property("name", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL),
        Property("type", _STRING, _OPTIONAL, "e.g. `technical`, `organizational`"),
        Property(
            "status",
            EnumType(("active", "deprecated", "merged")),
            _OPTIONAL,
            "merged marks a tombstone with an outbound MERGED_INTO edge; no status means active.",
        ),
        Property(
            "confidence",
            _CONFIDENCE,
            _REQUIRED,
            "Extractor's certainty in reusing or minting this Capability; always recorded.",
        ),
    ),
)

_POLICY = Node(
    label="Policy",
    properties=(
        Property("title", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL, _SHORT_SUMMARY_NOTE),
        Property("owner_id", _STRING, _OPTIONAL),
        # `proposed` is a lifecycle status (policy_lifecycle/rules.py); pinned by
        # tests/domain_schema/test_policy_status_matches_lifecycle.py.
        Property("status", EnumType(_GOVERNANCE_STATUSES), _REQUIRED),
        Property("version", _STRING, _OPTIONAL),
        Property("scope_in", _STRING, _OPTIONAL, "What this Policy's commitment covers."),
        Property(
            "scope_out",
            _STRING,
            _OPTIONAL,
            "What this Policy's commitment explicitly does not cover.",
        ),
        Property(
            "normative_commitments",
            _STRING,
            _OPTIONAL,
            ("The enforceable (`must`/`shall`/`required`) commitments this Policy makes."),
        ),
        Property("review_cadence", _STRING, _OPTIONAL, "Review interval or trigger condition."),
        Property(
            "exception_pathway",
            _STRING,
            _OPTIONAL,
            "The exception/risk-acceptance mechanism and who can grant it.",
        ),
        Property(
            "measurable_outcomes",
            _STRING,
            _OPTIONAL,
            "At least one quantifiable or objectively verifiable outcome.",
        ),
        Property(
            "capability_grouping_rationale",
            _STRING,
            _OPTIONAL,
            (
                "Why the governed Capabilities belong under one Policy — same owner, "
                "cadence, and control model."
            ),
        ),
    ),
)

_STANDARD = Node(
    label="Standard",
    properties=(
        Property("title", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL, _SHORT_SUMMARY_NOTE),
        Property(
            "implementation_status",
            EnumType(("draft", "implemented", "reviewed", "deprecated")),
            _REQUIRED,
        ),
        Property("status", EnumType(_GOVERNANCE_STATUSES), _REQUIRED, _GOVERNANCE_STATUS_NOTE),
        Property("version", _STRING, _OPTIONAL),
        Property(
            "procedure",
            _STRING,
            _OPTIONAL,
            ("Steps explicit enough that two implementers would execute them the same way."),
        ),
        Property(
            "implementer_role",
            _STRING,
            _OPTIONAL,
            ("Who implements. Standard has no `owner_id`; this is the only place this lives."),
        ),
        Property("reviewer_role", _STRING, _OPTIONAL, "Who reviews/verifies."),
        Property(
            "applicability_boundary",
            _STRING,
            _OPTIONAL,
            (
                "Which systems, environments, or data classes this Standard governs — "
                "and which it doesn't."
            ),
        ),
        Property(
            "verification_notes",
            _STRING,
            _OPTIONAL,
            (
                "Written so a Control could be built directly against it, pass/fail, "
                "without interpretation."
            ),
        ),
        Property(
            "change_rationale",
            _STRING,
            _OPTIONAL,
            (
                "Why this Standard was introduced, or what changed from its previous "
                "version and why."
            ),
        ),
    ),
)

_CONTROL = Node(
    label="Control",
    properties=(
        Property("type", EnumType(("automated", "manual")), _REQUIRED),
        Property("title", _STRING, _REQUIRED),
        Property("description", _STRING, _OPTIONAL, _SHORT_SUMMARY_NOTE),
        Property(
            "implementation_status",
            EnumType(("planned", "implemented", "reviewed", "deprecated")),
            _REQUIRED,
        ),
        Property("status", EnumType(_GOVERNANCE_STATUSES), _REQUIRED, _GOVERNANCE_STATUS_NOTE),
        Property("execution_frequency", _STRING, _OPTIONAL),
        Property("last_test_date", DateType(), _OPTIONAL),
        Property("next_review_date", DateType(), _OPTIONAL),
        Property(
            "evidence_ref",
            _STRING,
            _OPTIONAL,
            (
                "Opaque pointer into an external evidence/audit store; that store is "
                "out of scope for this document."
            ),
        ),
        Property(
            "pass_fail_criteria",
            _STRING,
            _OPTIONAL,
            "Success criteria precise enough that two executors reach the same verdict.",
        ),
        Property(
            "execution_method",
            _STRING,
            _OPTIONAL,
            (
                "How this Control runs, and its intended trigger/cadence — independent "
                "of whether `execution_frequency` is set yet."
            ),
        ),
        Property(
            "evidence_plan",
            _STRING,
            _OPTIONAL,
            (
                "What evidence this Control will produce and where, stated before "
                "`evidence_ref` exists."
            ),
        ),
        Property(
            "executor_role",
            _STRING,
            _OPTIONAL,
            ("Who executes. Control has no `owner_id`; this is the only place this lives."),
        ),
        Property("reviewer_role", _STRING, _OPTIONAL, "Who reviews the result."),
        Property(
            "risk_alignment_rationale",
            _STRING,
            _OPTIONAL,
            (
                "How this Control's objective addresses the risk exposure of the "
                "RiskPath(s) it verifies."
            ),
        ),
    ),
)

_SOURCE_REF = (EdgeProperty("source_ref", StringType(min_length=1), _REQUIRED),)
_CLASSIFICATION = Cardinality.parse("1..* : 0..*")
_CLASSIFICATION_RULE = "3 \u2014 classification layer"


def _edge(
    edge_type: str,
    source: str,
    target: str,
    cardinality: str | Cardinality,
    *,
    properties: tuple[EdgeProperty, ...] = (),
    note: str = "",
    provenance_rule: str = "",
) -> Edge:
    parsed = cardinality if isinstance(cardinality, Cardinality) else Cardinality.parse(cardinality)
    return Edge(
        type=edge_type,
        source=source,
        target=target,
        cardinality=parsed,
        properties=properties,
        note=note,
        provenance_rule=provenance_rule,
    )


# Order: Edge Catalog order, Policy SUPERSEDED_BY directly after the instrument one.
_EDGES = (
    _edge(
        "DEFINES",
        "RegulatoryInstrument",
        "Role",
        "1 : 0..*",
        properties=_SOURCE_REF,
        note=(
            "The article/section where this RegulatoryInstrument defines this Role. "
            "Lives on the edge, not on Role, because the defining act is specific "
            "to this RegulatoryInstrument\u2013Role pair."
        ),
        provenance_rule="1 \u2014 edge-owned",
    ),
    _edge(
        "EXPRESSES",
        "RegulatoryInstrument",
        "Requirement",
        "1 : 0..*",
        properties=_SOURCE_REF,
        note="Article/section where the instrument expresses the Requirement; lives on the edge.",
        provenance_rule=(
            "1 \u2014 edge-owned; also structurally fixed enough to double as Requirement's "
            "own identity, unlike Role's"
        ),
    ),
    _edge(
        "SUPERSEDED_BY",
        "RegulatoryInstrument",
        "RegulatoryInstrument",
        "0..1 : 0..1",
        note="Self-relationship tracking regulatory version succession.",
        provenance_rule="n/a \u2014 version succession, not a provenance fact",
    ),
    _edge(
        "SUPERSEDED_BY",
        "Policy",
        "Policy",
        "0..1 : 0..1",
        note="A Policy has at most one successor version; written when a fork is created.",
        provenance_rule="n/a \u2014 version succession, not a provenance fact",
    ),
    _edge(
        "TRANSPOSES",
        "RegulatoryInstrument",
        "RegulatoryInstrument",
        "0..* : 1",
        note="A national_transposition node links to the single directive it implements.",
        provenance_rule=(
            "n/a \u2014 structural bibliographic link (a national statute implements an EU "
            "directive), not a provenance fact; parallels `SUPERSEDED_BY`."
        ),
    ),
    _edge(
        "HAS",
        "Role",
        "Obligation",
        "1 : 0..*",
        note=(
            "A Role has zero or more Obligations assigned to it; each of those "
            "Obligations is borne by this Role alone."
        ),
        provenance_rule="n/a \u2014 structural assignment, no location fact involved.",
    ),
    _edge(
        "SATISFIED_BY",
        "Requirement",
        "Obligation",
        "1..* : 0..*",
        note="Bridges a Requirement to the Obligations that satisfy it; many-to-many.",
        provenance_rule="2 \u2014 recoverable via this Requirement's own `EXPRESSES` edge",
    ),
    _edge(
        "REQUIRES",
        "Obligation",
        "Capability",
        "1..* : 0..*",
        note="See [Capability](#capability).",
        provenance_rule="2 \u2014 recoverable transitively, one hop further than `SATISFIED_BY`",
    ),
    _edge(
        "COVERS",
        "PracticeArea",
        "Capability",
        _CLASSIFICATION,
        note="Classifies which reusable Capabilities belong to this practice area.",
        provenance_rule=_CLASSIFICATION_RULE,
    ),
    _edge(
        "OWNS",
        "PracticeArea",
        "Policy",
        _CLASSIFICATION,
        note="Assigns governance ownership of Policies by area.",
        provenance_rule=_CLASSIFICATION_RULE,
    ),
    _edge(
        "MITIGATED_BY",
        "RiskPath",
        "Capability",
        _CLASSIFICATION,
        note=("Connects abstract risk exposure to reusable technical/organizational capacity."),
        provenance_rule=_CLASSIFICATION_RULE,
    ),
    _edge(
        "VERIFIED_BY",
        "RiskPath",
        "Control",
        _CLASSIFICATION,
        note="Connects risk exposure to concrete verification evidence paths.",
        provenance_rule=_CLASSIFICATION_RULE,
    ),
    _edge(
        "MERGED_INTO",
        "Capability",
        "Capability",
        "0..1 : 0..*",
        note=("Present only on a `merged` tombstone; points at the survivor that absorbed it."),
        provenance_rule=(
            "n/a \u2014 redirect marker from a `merged` tombstone to its survivor, not a "
            "provenance fact; parallels `SUPERSEDED_BY`. Chains (a survivor later absorbed "
            "itself) are followed to the terminal Capability."
        ),
    ),
    _edge(
        "GOVERNED_BY",
        "Capability",
        "Policy",
        "0..* : 0..1",
        note=(
            "See [Policy → GOVERNED_BY](#policy). A Capability has exactly one "
            "governing Policy at any time, including while its Policy is being "
            "superseded."
        ),
        provenance_rule=(
            "3 if Policy is human-authored; 2 (recoverable via "
            "`REQUIRES`\u2192`SATISFIED_BY`\u2192`EXPRESSES`) if Policy is "
            "internal-SoP-derived."
        ),
    ),
    _edge(
        "SUPPORTED_BY",
        "Policy",
        "Standard",
        "1 : 1..*",
        note=(
            "See [Standard → SUPPORTED_BY](#standard). Every Policy requires at "
            "least one Standard defining how its commitment is actually "
            "implemented."
        ),
        provenance_rule=(
            "3 if Standard is human-authored; 2 (recoverable via `GOVERNED_BY` onward) if "
            "internal-SoP-derived"
        ),
    ),
    _edge(
        "IMPLEMENTED_BY",
        "Standard",
        "Control",
        "1 : 0..*",
        note="See [Control](#control).",
        provenance_rule=(
            "3 if Control is human-authored; 2 (recoverable via `SUPPORTED_BY` onward) if "
            "internal-SoP-derived"
        ),
    ),
)

DOMAIN_SCHEMA = Schema(
    nodes=(
        _REGULATORY_INSTRUMENT,
        _ROLE,
        _REQUIREMENT,
        _OBLIGATION,
        _PRACTICE_AREA,
        _RISK_PATH,
        _CAPABILITY,
        _POLICY,
        _STANDARD,
        _CONTROL,
    ),
    edges=_EDGES,
)
