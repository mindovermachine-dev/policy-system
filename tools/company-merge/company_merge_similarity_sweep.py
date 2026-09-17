#!/usr/bin/env python3
r"""Maintainer script: empirically validate Company Merge's semantic-match threshold (issue #29).

**Increment 1 of issue #29** (PLAN.md §1 Increment 1, CHANGES.md row M3's path
substitution to `tools/company-merge/` and row m2's AST import-scan test): the thinnest
possible real slice -- a labeled `SimilarityPair` shape, `score_pair()` scoring one pair
through the *exact* production path Company Merge itself uses
(`ps_service.llm_interface.embedding.route_embedding`, always fresh for `text_a` and
reused from `text_b_cached_embedding` when present for `text_b` -- issue #100 Slice 4,
CHANGES.md row F1 -- then `ps_service.company_merge.similarity.cosine_similarity`
directly on the two resulting vectors, never a reimplementation, AC-BI-004/AC-BI-005/
AC-BI-006), and a 3-candidate `build_dataset()` that mechanically proves the AC-BI-003
identical-Capability-name exclusion rule rather than merely omitting such a pair by hand.

**Increment 2** (PLAN.md §1 Increment 2, AC-BI-007): `sweep_dataset()` scores a whole
dataset against a whole range of candidate thresholds while fetching each unique text's
embedding at most once, via a single `dict[str, tuple[float, ...]]` cache
(`_embed()`) shared across every pair and every threshold.

**Increment 3** (PLAN.md §1 Increment 3, AC-BI-008): no `try`/`except` anywhere in
`_embed`/`sweep_dataset` -- a `LlmProviderError` from any `route_embedding` call
propagates unchanged, aborting the whole sweep with no partial `ThresholdResult` list,
mirroring `ps_service.company_merge.dedup.find_best_semantic_match`'s own documented
"no try/except in this function" (`dedup.py:181-182`) and `dedupe_canonical_nodes`'s
call-order-based "abort with no partial write" guarantee (`dedup.py:313-315`).

**Increment 4** (PLAN.md §1 Increment 4, CHANGES.md row M3's path substitution):
`build_dataset()` now returns the full labeled dataset -- 10 true-match pairs (PLAN.md
§3.2, human-authored name-style paraphrase grounded in a real node's own `description`
paired with that node's real `name`) and 11 true-non-match pairs (PLAN.md §3.1, two
real, distinct Capability nodes' `name` fields verbatim) -- every pair citing the real
GDPR/NIS2/CRA Capability node id(s) it is drawn from (AC-BI-001) with a non-empty
`rationale` (AC-BI-002). `build_dataset()` also runs a real mechanical exclusion guard
(AC-BI-003's general form, not just a handful of illustrative examples): it loads every
real Capability node from `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` at
construction time (issue #100), computes the set of `name` values shared by two or more
of the 778 real Capability node entries, and raises `SweepDatasetValidationError` if any
dataset pair's `text_a` and `text_b` are identical to each other AND that shared string
names two or more real corpus nodes -- proven against the whole 778-node curated-content
corpus, not merely asserted by convention.

**Increment 5** (PLAN.md §1 Increment 5 / §4 "CLI surface"/"Dependency injection seam",
CHANGES.md row M2): `main(argv=None, *, call_embedding=None, config_loader=load_config) ->
int` -- a re-runnable CLI. `--model` defaults to `config_loader().llm_interface_embed_model`
(AC-BI-005), failing closed with a stderr message and exit code `1` if neither `--model` nor
that config field is resolvable, mirroring `run_live_merge.py`'s `_resolve_merge_config`
pattern. `--threshold-start`/`--threshold-stop`/`--threshold-step` (default `0.70`/`0.95`/
`0.01`, PLAN.md/CHANGES.md row m3) build the candidate threshold range. `--dataset` is a
real CLI flag (unlike `call_embedding`/`config_loader`, which have no flag) taking a path to
a JSON file of `SimilarityPair`-shaped objects; when omitted, the module-level
`DEFAULT_DATASET` constant (`build_dataset()`'s full 21-pair set, computed once at import)
is used. `--recommendation-path` is parsed (default: this issue's own
`.orchestrator/tracker/issue-29-similarity-threshold/SWEEP_RECOMMENDATION.md`) but not yet
acted on -- the recommendation writer is a later increment; this script only prints the
per-threshold table to stdout. `configure_logging()` is called before `sweep_dataset()` (PLAN.md
§0 F5), and `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))` is this script's normal
entrypoint.

**Increment 6** (PLAN.md §1 Increment 6, AC-BI-009): `select_recommendation(results) ->
ThresholdResult` picks the highest-`f1` entry from a full sweep, breaking a tie toward
the HIGHER threshold (CHANGES.md row m3, biasing the recommendation toward precision --
fewer false Capability merges). `render_recommendation(result) -> str` renders that pick
as a short Markdown block naming the specific threshold value and its precision/recall/F1
(each to 3 decimal places) plus the TP/FP/TN/FN counts. `main()` now calls both after
`sweep_dataset()`, prints the rendered block to stdout in addition to the per-threshold
table, and writes it to `--recommendation-path` (default: this issue's own
`.orchestrator/tracker/issue-29-similarity-threshold/SWEEP_RECOMMENDATION.md`), creating
parent directories as needed.

Later increments (not implemented here -- see `.orchestrator/tracker/
issue-29-similarity-threshold/PLAN.md`) add the live sweep execution against the real
Azure embedding endpoint and the config/chart/docs reconciliation it feeds. This module
is exercised via `ps-service/tests/company_merge/
test_similarity_threshold_sweep_scoring.py`, `test_similarity_threshold_dataset.py`, and
`test_company_merge_similarity_sweep_cli.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from ps_service.company_merge.similarity import cosine_similarity
from ps_service.config import load_config
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from ps_service.config import ServiceConfig
    from ps_service.export.models import SerializedNode
    from ps_service.llm_interface.client import EmbeddingCaller


@dataclass(frozen=True, slots=True)
class SimilarityPair:
    """One labeled Capability-name pair for the similarity-threshold sweep dataset.

    `text_a`/`text_b` are name-shaped strings -- either a real Capability node's own
    `name` field, verbatim, or a human-authored name-style paraphrase -- never a
    description sentence, because `route_embedding` is only ever called on a
    Capability's `name` in production (`ps_service.company_merge.dedup`'s
    `_TEXT_PROPERTY_BY_LABEL`).

    `text_b_cached_embedding` mirrors `ExistingCanonicalNode.embedding` in
    `ps_service.company_merge.dedup.find_best_semantic_match`'s own reuse-if-cached
    treatment (`dedup.py:194-201`): `text_b` plays the "existing candidate" role in
    every pair, so when the real curated-content node it cites already carries a
    cached `embedding`, that cached vector is reused instead of a fresh
    `route_embedding` call (issue #100 Slice 4, CHANGES.md row F1). `text_a` always
    plays the "incoming" role and is never short-circuited this way, regardless of
    whether its own underlying node (for a non-match pair) has a real cached
    embedding too -- mirroring `incoming_text`'s always-fresh treatment
    (`dedup.py:187-190`). `None` here (the default, and always the case for a
    `--dataset` override file loaded via `_load_dataset_from_path`) falls back to a
    fresh fetch for `text_b`, exactly like `entry.embedding is None` in production.
    """

    text_a: str
    text_b: str
    label: Literal["match", "non_match"]
    cited_node_ids: tuple[str, ...]
    rationale: str
    text_b_cached_embedding: tuple[float, ...] | None = None


def score_pair(
    pair: SimilarityPair,
    *,
    model: str,
    call_embedding: EmbeddingCaller | None = None,
) -> float:
    """Score `pair` through Company Merge's real production scoring path.

    `text_a` is always resolved fresh via `route_embedding` with the given `model`
    (AC-BI-005 -- "the same model config Company Merge uses in production"). `text_b`
    reuses `pair.text_b_cached_embedding` when present, only calling `route_embedding`
    when it is `None` -- mirroring `find_best_semantic_match`'s reuse-if-cached
    treatment of an existing canonical entry's embedding (`dedup.py:194-201`; issue
    #100 Slice 4, CHANGES.md row F1). Either way, `ps_service.company_merge.similarity.
    cosine_similarity` is called directly on the two resulting vectors (AC-BI-004 --
    never a reimplementation). `call_embedding` defaults to `None`, which
    `route_embedding` itself resolves to the real `default_embedding_caller`
    (AC-BI-006 -- no new endpoint/credential/dependency is ever constructed here). A
    `LlmProviderError` from either `route_embedding` call propagates unchanged -- no
    try/except in this function.
    """
    embedding_a = tuple(
        route_embedding(pair.text_a, model=model, call_embedding=call_embedding).vector
    )
    embedding_b = (
        pair.text_b_cached_embedding
        if pair.text_b_cached_embedding is not None
        else tuple(route_embedding(pair.text_b, model=model, call_embedding=call_embedding).vector)
    )
    return cosine_similarity(embedding_a, embedding_b)


def _is_identical_name_pair(text_a: str, text_b: str) -> bool:
    """AC-BI-003's exclusion rule: identical Capability-name strings never reach scoring.

    Mirrors Company Merge's own exact-key match (`ps_service.company_merge.dedup`),
    which resolves an identical name before `find_best_semantic_match`'s
    embedding/cosine-similarity path ever runs -- scoring such a pair here would test
    nothing about the semantic threshold being validated.
    """
    return text_a == text_b


class SweepDatasetValidationError(Exception):
    """`build_dataset()`'s dataset violates AC-BI-003's general exclusion rule.

    Raised when a dataset pair's `text_a`/`text_b` are identical to each other AND
    that shared string is a real Capability `name` present on two or more nodes across
    `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` -- such a pair would resolve
    via Company Merge's own exact-key match before semantic scoring ever runs, so it
    must never reach the shipped dataset (PLAN.md §1 Increment 4, §3.3; issue #100).
    """


_TOOLS_COMPANY_MERGE_DIR = Path(__file__).resolve().parent


class CuratedContentCorpusError(Exception):
    """A `curated-content/{id}/baseline.json` file is missing or fails to parse.

    Raised by `_load_curated_content_capability_nodes()` (AC-BI-003) -- never
    returns an empty or partial Capability node list silently. Follows this
    component's domain-specific-exception convention (mirrors
    `SweepDatasetValidationError`/`SweepDatasetFileError` in this same module,
    `docs/coding-standards/level2-python-instructions.md`'s "domain-specific
    exception types per component" rule) rather than raising a bare
    `Exception`/`ValueError`/`FileNotFoundError`.
    """


_CURATED_CONTENT_DIR = _TOOLS_COMPANY_MERGE_DIR.parent.parent / "curated-content"
_CURATED_CONTENT_INSTRUMENT_DIRS = ("GDPR-1.0", "NIS2-1.0", "CRA-1.0")


def _load_curated_content_capability_nodes() -> tuple[SerializedNode, ...]:
    """Load every Capability node from the three curated-content baseline exports.

    Reads `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` (AC-BI-001) via
    `export.serialize.parse_serialized_graph_json` -- never a hand-rolled
    `json.loads` of the node shape, and never a stale spike-era fixture directory.
    Filters the returned `SerializedGraph.nodes` to `label == "Capability"`.

    Raises `CuratedContentCorpusError` if any of the three `baseline.json` files is
    missing or fails to parse as a `SerializedGraph` (AC-BI-003) -- this function
    never returns an empty or partial result for a missing/malformed file; a
    failure on any one of the three instruments aborts the whole load.

    Wired in as `_repeated_capability_names_from_corpus`'s body (issue #100 Slice 2)
    -- its only in-module caller.
    """
    nodes: list[SerializedNode] = []
    for instrument_dir in _CURATED_CONTENT_INSTRUMENT_DIRS:
        path = _CURATED_CONTENT_DIR / instrument_dir / "baseline.json"
        if not path.is_file():
            raise CuratedContentCorpusError(
                f"{path} does not exist -- build_dataset() requires all three "
                "curated-content baseline.json files (AC-BI-001/AC-BI-003)"
            )
        try:
            graph = parse_serialized_graph_json(path.read_bytes())
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CuratedContentCorpusError(
                f"{path} could not be parsed as a SerializedGraph: {exc}"
            ) from exc
        nodes.extend(node for node in graph.nodes if node.label == "Capability")
    return tuple(nodes)


def _capability_nodes_by_id() -> dict[str, SerializedNode]:
    """Index every real curated-content Capability node by its `id` property.

    Built from `_load_curated_content_capability_nodes()` -- used by `build_dataset()`
    to look up each spec's `text_b`-side node and attach its cached `embedding`
    (issue #100 Slice 4, CHANGES.md row F1), mirroring
    `ps_service.company_merge.dedup.find_best_semantic_match`'s reuse-if-cached
    treatment of `ExistingCanonicalNode.embedding` (`dedup.py:194-201`). When the same
    id is present in more than one loaded instrument file (the 10 cross-regulation
    shared-id groups), the last one loaded wins -- both entries represent the same
    real capability with the same cached embedding in practice, so this is not a
    meaningful ambiguity.
    """
    return {
        cast("str", node.properties["id"]): node
        for node in _load_curated_content_capability_nodes()
    }


def _text_b_cached_embedding(
    cited_node_ids: tuple[str, ...], nodes_by_id: dict[str, SerializedNode]
) -> tuple[float, ...] | None:
    """The `text_b`-side cited node's own cached `embedding`, if present.

    `cited_node_ids[-1]` is always the id corresponding to `text_b` by this module's
    existing tuple-ordering convention -- every `_MATCH_PAIR_SPECS` entry's single id
    names `text_b`'s own node, and every `_NON_MATCH_PAIR_SPECS` entry orders
    `text_a`'s node id first and `text_b`'s node id second. Returns `None` if the node
    is missing from `nodes_by_id` or carries no `embedding` property --
    `_score_dataset`/`score_pair` fall back to a fresh fetch in that case, mirroring
    `entry.embedding is None` in `dedup.py:195-200`.
    """
    node = nodes_by_id.get(cited_node_ids[-1])
    if node is None:
        return None
    embedding = node.properties.get("embedding")
    if embedding is None:
        return None
    return tuple(cast("list[float]", embedding))


def _repeated_capability_names_from_corpus() -> frozenset[str]:
    """Capability `name` values shared by two or more curated-content nodes.

    Sourced from `_load_curated_content_capability_nodes()` (curated-content,
    AC-BI-002/AC-BI-007) -- never a stale spike-era fixture directory. Returns the
    `name` values shared by two or more of the 778 real Capability nodes across
    `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` (10 such names, confirmed
    by direct count during planning -- every one of the 10 is the *same* node id
    cited from more than one regulation's extraction, never two distinct nodes that
    happen to share a name). `_assert_dataset_excludes_real_identical_name_pairs`
    checks the shipped dataset against this set, at full curated-content scale.
    """
    names = (
        cast("str", node.properties["name"]) for node in _load_curated_content_capability_nodes()
    )
    counts = Counter(names)
    return frozenset(name for name, count in counts.items() if count >= 2)


def _assert_dataset_excludes_real_identical_name_pairs(
    pairs: tuple[SimilarityPair, ...], repeated_names: frozenset[str]
) -> None:
    """AC-BI-003, proven at corpus scale.

    Raise if any pair is a same-string pair drawn from a Capability name that two or
    more real corpus nodes share. A real mechanical check, not a manual curation
    discipline -- `repeated_names` is
    computed from all 778 real Capability nodes across curated-content
    (`_repeated_capability_names_from_corpus`), so this catches any such pair
    regardless of which regulation(s) it came from, not just the handful of names
    cited by name in PLAN.md's prose.
    """
    violations = tuple(
        pair
        for pair in pairs
        if _is_identical_name_pair(pair.text_a, pair.text_b) and pair.text_a in repeated_names
    )
    if violations:
        violating_ids = tuple(pair.cited_node_ids for pair in violations)
        raise SweepDatasetValidationError(
            f"{len(violations)} dataset pair(s) use a Capability name string shared by "
            "two or more real corpus nodes as both sides of the same pair -- AC-BI-003 "
            "requires these be excluded, since they resolve via Company Merge's own "
            f"exact-key match before semantic scoring ever runs: {violating_ids!r}"
        )


_CandidateSpec = tuple[str, str, Literal["match", "non_match"], tuple[str, ...], str]

# 10 true-match pairs (PLAN.md §3.2, Slice 3): each is (human-authored name-style
# paraphrase, the real node's own `name`, "match", the real node's id, a rationale
# grounded in the real node's own `description`). Every real name/id/description cited
# below was independently re-verified against the real
# `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` files for this dispatch (issue
# #100 Slice 3) -- not copied blindly from PLAN.md's prose.
_MATCH_PAIR_SPECS: tuple[_CandidateSpec, ...] = (
    (
        "Assessing and Mitigating Privacy Risks Before High-Risk Processing Begins",
        "Data Protection Impact Assessment",
        "match",
        ("cap_data_protection_impact_assessment_1e2cf0",),
        (
            "Paraphrase grounded in the real node's own description: 'assess privacy "
            "and data protection risks of a proposed processing activity and identify "
            "measures to mitigate them.' (GDPR)"
        ),
    ),
    (
        "Assessing a Personal Data Breach and Notifying the Supervisory Authority in Time",
        "Breach Notification Workflow",
        "match",
        ("cap_breach_notification_workflow_633423",),
        (
            "Paraphrase grounded in the real node's own description: 'assessing a "
            "personal data breach and notifying the supervisory authority within "
            "required timeframes.' (GDPR)"
        ),
    ),
    (
        "Appointing and Formally Designating a Data Protection Officer",
        "Data Protection Officer Designation",
        "match",
        ("cap_data_protection_officer_designation_792f61",),
        (
            "Paraphrase grounded in the real node's own description: 'appointing and "
            "formally designating a Data Protection Officer, including making the "
            "designation effective within the organization.' (GDPR)"
        ),
    ),
    (
        "Formal Review and Sign-Off of Binding Corporate Rules for Intra-Group Transfers",
        "Binding Corporate Rules Approval",
        "match",
        ("cap_binding_corporate_rules_approval_026747",),
        (
            "Paraphrase grounded in the real node's own description: 'formal review "
            "and approval process for binding corporate rules used for intra-group "
            "personal data transfers.' (GDPR)"
        ),
    ),
    (
        "Maintaining an Internal Register of Processing Operations and Their Details",
        "Record of Processing Activities",
        "match",
        ("cap_record_of_processing_activities_1f3d70",),
        (
            "Paraphrase grounded in the real node's own description: 'maintaining an "
            "internal record of processing operations, including required details "
            "such as purposes, categories, recipients, transfers, and retention "
            "information.' (GDPR)"
        ),
    ),
    (
        "Preparing For, Responding To, and Recovering From Cyber Incidents",
        "Incident Response and Recovery",
        "match",
        ("cap_incident_response_and_recovery_182736",),
        (
            "Paraphrase grounded in the real node's own description: 'preparing for "
            "cyber incidents, responding to them, and restoring normal operations "
            "afterward.' (NIS2)"
        ),
    ),
    (
        "Identifying, Assessing and Monitoring Network and Information System Security Risks",
        "Risk Management",
        "match",
        ("cap_risk_management_2cf786",),
        (
            "Paraphrase grounded in the real node's own description: 'identifying, "
            "assessing, mitigating, and monitoring network and information system "
            "security risks.' (NIS2)"
        ),
    ),
    (
        "Setting Up National or Sectoral Computer Security Incident Response Teams",
        "CSIRT Establishment",
        "match",
        ("cap_csirt_establishment_b4eb94",),
        (
            "Paraphrase grounded in the real node's own description: 'establish "
            "national or sectoral computer security incident response teams (CSIRTs) "
            "to handle cybersecurity incidents.' (NIS2)"
        ),
    ),
    (
        "Determining When CE Marking Applies and Meeting Its Marking and Documentation Duties",
        "CE Marking Compliance",
        "match",
        ("cap_ce_marking_compliance_8d32e6",),
        (
            "Paraphrase grounded in the real node's own description: 'determining "
            "when CE marking applies and ensuring products meet the required "
            "conformity assessment, marking, and documentation obligations.' (CRA)"
        ),
    ),
    (
        "Evaluating Whether a Product Meets the Required Essential Cybersecurity Requirements",
        "Conformity Assessment",
        "match",
        ("cap_conformity_assessment_a2edfc",),
        (
            "Paraphrase grounded in the real node's own description: 'evaluating "
            "whether a product or service meets specified essential cybersecurity "
            "requirements.' (CRA)"
        ),
    ),
)

# 11 true-non-match pairs (PLAN.md §3.1, Slice 3, with CHANGES.md row F2's 2-pair
# substitution already applied): each cites two real, distinct Capability nodes' own
# `name` fields verbatim, with a rationale explaining why the extraction kept them as
# separate capabilities despite shared vocabulary. Every real name/id/description
# cited below was independently re-verified against the real
# `curated-content/{GDPR,NIS2,CRA}-1.0/baseline.json` files for this dispatch (issue
# #100 Slice 3).
_NON_MATCH_PAIR_SPECS: tuple[_CandidateSpec, ...] = (
    (
        "Confidentiality Control",
        "Confidentiality Protection",
        "non_match",
        ("cap_confidentiality_control_924e03", "cap_confidentiality_protection_19b5db"),
        (
            "Both are near-identically-named CRA nodes ('Control' vs 'Protection') "
            "guarding against unauthorized disclosure, but the extraction kept them as "
            "distinct capabilities -- a lexical-overlap hard negative within a single "
            "regulation."
        ),
    ),
    (
        "Confidentiality Safeguards",
        "Confidentiality Obligation",
        "non_match",
        ("cap_confidentiality_safeguards_d03009", "cap_confidentiality_obligation_ac113b"),
        (
            "Cross-regulation (CRA vs GDPR): Confidentiality Safeguards protects "
            "sensitive security-related information from disclosure to unauthorized "
            "parties, while Confidentiality Obligation is the legal/contractual duty "
            "binding authorized persons to secrecy -- a mechanism vs. a duty, kept "
            "distinct."
        ),
    ),
    (
        "Appeal Procedure",
        "Appeals Process Management",
        "non_match",
        ("cap_appeal_procedure_fce818", "cap_appeals_process_management_a60040"),
        (
            "CRA's own extraction created several near-duplicate appeal-related "
            "Capability nodes; these two ('Procedure' vs 'Process Management') are "
            "kept distinct despite near-identical naming -- proves the sweep must not "
            "recommend a threshold that would collapse genuinely distinct extraction "
            "artifacts."
        ),
    ),
    (
        "Assurance Level Assessment",
        "Assurance Level Framework",
        "non_match",
        ("cap_assurance_level_assessment_1aea8b", "cap_assurance_level_framework_940c57"),
        (
            "CRA: Assessment assigns the assurance level to a specific product using "
            "the criteria; Framework defines the process and criteria themselves -- "
            "an execution-vs-definition distinction kept as separate capabilities."
        ),
    ),
    (
        "Administrative Burden Assessment",
        "Administrative Burden Management",
        "non_match",
        (
            "cap_administrative_burden_assessment_821831",
            "cap_administrative_burden_management_97c3b0",
        ),
        (
            "CRA: Assessment evaluates and controls administrative effort generally; "
            "Management specifically reduces and keeps requirements proportionate for "
            "micro/SME entities -- adjacent but distinct scopes kept separate."
        ),
    ),
    (
        # CHANGES.md row F2 substitution (replaces the original "Administrative Fine
        # Framework" / "Administrative Fines Framework" pair, which was a mislabeled
        # duplicate, not a genuine hard negative).
        "Information Sharing Policy",
        "Information Sharing Tools",
        "non_match",
        ("cap_information_sharing_policy_c0ac5d", "cap_information_sharing_tools_d1ec45"),
        (
            "NIS2: Information Sharing Policy is the governance capability -- the rules "
            "and permissions governing voluntary exchange of cybersecurity information "
            "with trusted parties -- while Information Sharing Tools is the technical/"
            "procedural mechanism (portals, channels) that implements that exchange. A "
            "policy-vs-tooling distinction kept as separate capabilities, verified "
            "against the real descriptions, not a paraphrase."
        ),
    ),
    (
        # CHANGES.md row F2 substitution (replaces the original "Authority Designation
        # Process" / "Authority Designation Workflow" pair, which was a mislabeled
        # duplicate -- the two nodes shared a byte-identical description).
        "CE Marking Display",
        "CE Marking Labeling",
        "non_match",
        ("cap_ce_marking_display_b729c1", "cap_ce_marking_labeling_890244"),
        (
            "CRA: CE Marking Display makes CE marking/compliance information visible on "
            "the product's website (a digital-disclosure capability); CE Marking "
            "Labeling controls the application of required identification markings/"
            "labels on the physical product itself (a physical-labeling-control "
            "capability). Distinct channels -- online disclosure vs. physical product "
            "labeling -- kept separate despite the shared 'CE Marking ___' name "
            "template, verified against the real descriptions."
        ),
    ),
    (
        "Conflict of Interest Management",
        "Conflict of Interest Control",
        "non_match",
        (
            "cap_conflict_of_interest_management_c331b1",
            "cap_conflict_of_interest_control_446c36",
        ),
        (
            "Cross-regulation (this exact 'Conflict of Interest Management' node is "
            "cited from both GDPR and CRA, see the 10 cross-regulation shared-id "
            "groups); 'Management' handles conflicts between assigned duties and "
            "personal/organizational interests generally, while 'Control' (CRA-only) "
            "is scoped specifically to conformity-assessment activities -- a "
            "general-vs-scoped distinction kept separate."
        ),
    ),
    (
        "Redundancy Management",
        "Redundant Systems",
        "non_match",
        ("cap_redundancy_management_2dc45c", "cap_redundant_systems_5232f2"),
        (
            "NIS2: Redundancy Management is the governance/process capability that "
            "maintains backup systems; Redundant Systems is the technical duplicated "
            "systems themselves -- a process-vs-artifact distinction kept separate."
        ),
    ),
    (
        "Regulatory Notification Workflow",
        "Regulatory Reporting Workflow",
        "non_match",
        (
            "cap_regulatory_notification_workflow_37f17e",
            "cap_regulatory_reporting_workflow_52af1f",
        ),
        (
            "Both 'Regulatory ... Workflow'-named and both cross-regulation-cited "
            "(Notification: GDPR/NIS2/CRA; Reporting: NIS2/CRA), but Notification is "
            "an event-driven duty to notify promptly when a reportable event arises, "
            "while Reporting is a periodic/compiled-information duty -- distinct "
            "obligations despite the shared 'Regulatory ... Workflow' template name."
        ),
    ),
    (
        "Purpose Limitation",
        "Service Continuity Planning",
        "non_match",
        ("cap_purpose_limitation_f14d50", "cap_service_continuity_planning_014921"),
        (
            "An unambiguous negative anchor (GDPR data-protection principle vs NIS2 "
            "operational-resilience planning) -- different regulatory domains, "
            "near-zero expected similarity -- confirms the sweep isn't only testing "
            "hard/near-duplicate cases."
        ),
    ),
)

_CANDIDATE_SPECS: tuple[_CandidateSpec, ...] = _MATCH_PAIR_SPECS + _NON_MATCH_PAIR_SPECS


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    """Precision/recall/F1 and confusion-matrix counts for one candidate threshold.

    Produced by `sweep_dataset()` for each threshold in its `thresholds` argument,
    scored against the same dataset and the same shared embedding cache (AC-BI-007).
    """

    threshold: float
    precision: float
    recall: float
    f1: float
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int


def _embed(
    text: str,
    cache: dict[str, tuple[float, ...]],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None,
) -> tuple[float, ...]:
    """Return `text`'s embedding, fetching it via `route_embedding` at most once.

    Checks `cache` first (keyed by the exact `text` string); on a miss, calls
    `route_embedding` and stores the resulting vector before returning it -- every
    caller shares the same `cache` dict, so a text repeated across pairs and/or
    thresholds is fetched exactly once for the lifetime of that cache (AC-BI-007). A
    `LlmProviderError` from `route_embedding` propagates unchanged -- no try/except in
    this function (AC-BI-008), mirroring `find_best_semantic_match`'s own documented
    behavior (`ps_service.company_merge.dedup`, `dedup.py:181-182`).
    """
    cached = cache.get(text)
    if cached is not None:
        return cached
    result = route_embedding(text, model=model, call_embedding=call_embedding)
    vector = tuple(result.vector)
    cache[text] = vector
    return vector


def _score_dataset(
    pairs: tuple[SimilarityPair, ...],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None,
) -> tuple[tuple[SimilarityPair, float], ...]:
    """Score every pair once, resolving `text_a` fresh and `text_b` reused-if-cached.

    `text_a` (the "incoming" side of every pair) is always resolved through `_embed`,
    sharing one embedding cache local to this `sweep_dataset()` call, populated lazily
    and reused across every pair -- never a direct `route_embedding` call from this
    loop for `text_a`. `text_b` (the "existing candidate" side) reuses
    `pair.text_b_cached_embedding` when present; only when it is `None` is `text_b`
    resolved through the same shared `_embed`/cache path. This mirrors
    `find_best_semantic_match`'s own `incoming_text`-always-fresh /
    `existing_index`-entry-reuse-if-cached split (`dedup.py:187-201`; issue #100
    Slice 4, CHANGES.md row F1).
    """
    cache: dict[str, tuple[float, ...]] = {}
    scored: list[tuple[SimilarityPair, float]] = []
    for pair in pairs:
        embedding_a = _embed(pair.text_a, cache, model=model, call_embedding=call_embedding)
        if pair.text_b_cached_embedding is not None:
            embedding_b = pair.text_b_cached_embedding
        else:
            embedding_b = _embed(pair.text_b, cache, model=model, call_embedding=call_embedding)
        scored.append((pair, cosine_similarity(embedding_a, embedding_b)))
    return tuple(scored)


def _threshold_result(
    scored_pairs: tuple[tuple[SimilarityPair, float], ...], threshold: float
) -> ThresholdResult:
    true_positive = 0
    false_positive = 0
    true_negative = 0
    false_negative = 0
    for pair, score in scored_pairs:
        predicted_match = score >= threshold
        if pair.label == "match" and predicted_match:
            true_positive += 1
        elif pair.label == "non_match" and predicted_match:
            false_positive += 1
        elif pair.label == "match" and not predicted_match:
            false_negative += 1
        else:
            true_negative += 1

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive) > 0
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative) > 0
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return ThresholdResult(
        threshold=threshold,
        precision=precision,
        recall=recall,
        f1=f1,
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
    )


def sweep_dataset(
    pairs: tuple[SimilarityPair, ...],
    thresholds: Iterable[float],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None = None,
) -> list[ThresholdResult]:
    """Score `pairs` once, then tally precision/recall/F1 at every threshold.

    Builds exactly one embedding cache for this call (via `_embed`), populated lazily
    as `pairs` are scored, and reused across every pair AND every threshold in
    `thresholds` -- each unique text is fetched via `route_embedding` at most once
    regardless of how many pairs reference it or how many thresholds are swept
    (AC-BI-007).

    A `LlmProviderError` from any `route_embedding` call propagates unchanged -- no
    try/except anywhere in this function or in `_embed`/`_score_dataset` (AC-BI-008).
    Because every pair is scored before any `ThresholdResult` is produced, a failure
    partway through scoring aborts the whole sweep: no `ThresholdResult` is returned
    for any threshold, never a partial list from whatever pairs happened to score
    before the failure.
    """
    scored_pairs = _score_dataset(pairs, model=model, call_embedding=call_embedding)
    return [_threshold_result(scored_pairs, threshold) for threshold in thresholds]


def build_dataset() -> tuple[SimilarityPair, ...]:
    """The full labeled dataset (PLAN.md §1 Increment 4).

    10 true-match pairs + 11 true-non-match pairs, every pair citing real GDPR/NIS2/CRA
    Capability node id(s) (AC-BI-001) with a non-empty rationale (AC-BI-002). Before
    returning, mechanically re-proves AC-BI-003's general form against the
    whole 778-node curated-content corpus (`_assert_dataset_excludes_real_identical_
    name_pairs`, construction-time, raising `SweepDatasetValidationError` if violated)
    rather than relying on the dataset having been curated by hand correctly -- see
    that function's docstring.

    Also attaches `text_b_cached_embedding` to every pair (issue #100 Slice 4,
    CHANGES.md row F1): each spec's `text_b`-side node (`cited_node_ids[-1]`) is
    looked up in the real curated-content corpus, and its own cached `embedding` is
    reused rather than fetched fresh when scored -- see `_text_b_cached_embedding`'s
    docstring.
    """
    nodes_by_id = _capability_nodes_by_id()
    pairs = tuple(
        SimilarityPair(
            text_a=text_a,
            text_b=text_b,
            label=label,
            cited_node_ids=cited_node_ids,
            rationale=rationale,
            text_b_cached_embedding=_text_b_cached_embedding(cited_node_ids, nodes_by_id),
        )
        for text_a, text_b, label, cited_node_ids, rationale in _CANDIDATE_SPECS
    )
    _assert_dataset_excludes_real_identical_name_pairs(
        pairs, _repeated_capability_names_from_corpus()
    )
    return pairs


DEFAULT_DATASET: tuple[SimilarityPair, ...] = build_dataset()

DEFAULT_THRESHOLD_START = 0.70
DEFAULT_THRESHOLD_STOP = 0.95
DEFAULT_THRESHOLD_STEP = 0.01

_REPO_ROOT = _TOOLS_COMPANY_MERGE_DIR.parent.parent
_DEFAULT_RECOMMENDATION_PATH = (
    _REPO_ROOT
    / ".orchestrator"
    / "tracker"
    / "issue-29-similarity-threshold"
    / "SWEEP_RECOMMENDATION.md"
)

_VALID_LABELS: tuple[Literal["match", "non_match"], ...] = ("match", "non_match")


class SweepDatasetFileError(Exception):
    """A `--dataset` override file could not be parsed into `SimilarityPair`s."""


def _load_dataset_from_path(path: Path) -> tuple[SimilarityPair, ...]:
    """Load a `--dataset` override file: a JSON array of `SimilarityPair`-shaped objects.

    Each entry requires `text_a`, `text_b`, `label` (`"match"`/`"non_match"`),
    `cited_node_ids` (a list of strings), and `rationale`. This is the mechanism a test
    uses to substitute a small, hermetic fixture for the CLI's own `--dataset` flag --
    never a way to skip AC-BI-003's exclusion guard, which only `build_dataset()` runs (a
    dataset file loaded this way is trusted as already-curated, exactly like Increment 4's
    own hand-authored `_CANDIDATE_SPECS`).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[SimilarityPair] = []
    for entry in cast("list[dict[str, object]]", raw):
        label = entry["label"]
        if label not in _VALID_LABELS:
            raise SweepDatasetFileError(
                f"{path}: pair has invalid label {label!r}, expected one of {_VALID_LABELS!r}"
            )
        pairs.append(
            SimilarityPair(
                text_a=cast("str", entry["text_a"]),
                text_b=cast("str", entry["text_b"]),
                label=label,
                cited_node_ids=tuple(cast("list[str]", entry.get("cited_node_ids", []))),
                rationale=cast("str", entry["rationale"]),
            )
        )
    return tuple(pairs)


def _threshold_range(start: float, stop: float, step: float) -> list[float]:
    """Inclusive threshold range from `start` to `stop`, stepping by `step`.

    Rounds each generated value to 10 decimal places to avoid float-accumulation noise
    (e.g. `0.7000000000000001`) -- well beyond the precision any real `--threshold-step`
    would use, so this never masks a genuine difference between candidate thresholds.
    """
    if step <= 0:
        raise ValueError(f"--threshold-step must be positive, got {step}")
    count = round((stop - start) / step) + 1
    return [round(start + step * i, 10) for i in range(count)]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Embedding model passed to route_embedding for every pair (default: "
        "config_loader().llm_interface_embed_model, i.e. PS_LLMINTERFACE_EMBED_MODEL)",
    )
    parser.add_argument(
        "--threshold-start",
        type=float,
        default=DEFAULT_THRESHOLD_START,
        help=f"First candidate threshold to sweep (default: {DEFAULT_THRESHOLD_START})",
    )
    parser.add_argument(
        "--threshold-stop",
        type=float,
        default=DEFAULT_THRESHOLD_STOP,
        help=f"Last candidate threshold to sweep, inclusive (default: {DEFAULT_THRESHOLD_STOP})",
    )
    parser.add_argument(
        "--threshold-step",
        type=float,
        default=DEFAULT_THRESHOLD_STEP,
        help=f"Increment between candidate thresholds (default: {DEFAULT_THRESHOLD_STEP})",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Path to a JSON file of SimilarityPair-shaped objects overriding the built-in "
        "21-pair DEFAULT_DATASET",
    )
    parser.add_argument(
        "--recommendation-path",
        type=Path,
        default=_DEFAULT_RECOMMENDATION_PATH,
        help="Where the rendered recommendation Markdown is written (default: this "
        "issue's own tracker dir); parent directories are created if needed",
    )
    return parser.parse_args(argv)


def _resolve_model(
    args: argparse.Namespace, config_loader: Callable[[], ServiceConfig]
) -> str | int:
    """Fail-closed `--model` resolution. An `int` return is the process exit code.

    Mirrors `run_live_merge.py`'s `_resolve_merge_config` pattern: an explicit `--model`
    always wins; otherwise `config_loader()` (the real `load_config` by default) must
    resolve `llm_interface_embed_model` (AC-BI-005 -- "the same model config Company Merge
    uses in production"), or the script exits with a clear stderr message rather than
    silently falling back to some other value.
    """
    if args.model is not None:
        return cast("str", args.model)
    config = config_loader()
    if config.llm_interface_embed_model is None:
        print(
            "--model was not given and PS_LLMINTERFACE_EMBED_MODEL is not resolvable via "
            "config_loader() -- set PS_LLMINTERFACE_EMBED_MODEL or pass --model explicitly.",
            file=sys.stderr,
        )
        return 1
    return config.llm_interface_embed_model


def _print_threshold_table(results: list[ThresholdResult]) -> None:
    """Print one row per `ThresholdResult` to stdout.

    Columns: threshold, precision, recall, f1, and the TP/FP/TN/FN confusion-matrix
    counts. One line per threshold, easy to grep/parse.
    """
    print("threshold  precision  recall     f1         tp   fp   tn   fn")
    for result in results:
        print(
            f"{result.threshold:<11.4f}"
            f"{result.precision:<11.4f}"
            f"{result.recall:<11.4f}"
            f"{result.f1:<11.4f}"
            f"{result.true_positive:<5d}"
            f"{result.false_positive:<5d}"
            f"{result.true_negative:<5d}"
            f"{result.false_negative:<5d}"
        )


def select_recommendation(results: list[ThresholdResult]) -> ThresholdResult:
    """Pick the recommended threshold from a full sweep's results (AC-BI-009).

    Selection rule: the entry with the highest `f1`. **Tie-break: on equal `f1`, the
    HIGHER `threshold` wins** (CHANGES.md row m3, accepted as-is) -- this deliberately
    biases the recommendation toward precision/fewer false merges when two or more
    thresholds score identically on F1, consistent with the domain's stated
    anti-over-merging preference (PLAN.md Open Question 3 / CHANGES.md row m3). This is
    not an incidental consequence of iteration order: `max()`'s key is the
    `(f1, threshold)` tuple, so the higher-threshold candidate always wins a tie
    regardless of `results`' input order.

    Raises `ValueError` if `results` is empty -- there is nothing to recommend.
    """
    if not results:
        raise ValueError("select_recommendation requires at least one ThresholdResult")
    return max(results, key=lambda result: (result.f1, result.threshold))


def render_recommendation(result: ThresholdResult) -> str:
    """Render `result` as a short Markdown block naming the recommended threshold.

    Names the specific recommended `threshold` value and its precision/recall/F1
    (each formatted to a fixed 3 decimal places -- AC-BI-009's "backed by the
    precision/recall/F1 figures measured at that value"), plus the TP/FP/TN/FN
    confusion-matrix counts, so a human reading `SWEEP_RECOMMENDATION.md` understands
    exactly what was measured and why this value was chosen (`select_recommendation`'s
    higher-threshold-on-tie rule).
    """
    return (
        "# Company Merge similarity-threshold sweep recommendation\n"
        "\n"
        f"**Recommended threshold: {result.threshold:.3f}**\n"
        "\n"
        f"- Precision: {result.precision:.3f}\n"
        f"- Recall: {result.recall:.3f}\n"
        f"- F1: {result.f1:.3f}\n"
        "\n"
        "Confusion matrix at this threshold:\n"
        "\n"
        f"- True positive: {result.true_positive}\n"
        f"- False positive: {result.false_positive}\n"
        f"- True negative: {result.true_negative}\n"
        f"- False negative: {result.false_negative}\n"
        "\n"
        "Selected as the highest-F1 threshold from the sweep; ties are broken toward "
        "the higher threshold, biasing the recommendation toward precision (fewer "
        "false Capability merges).\n"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    call_embedding: EmbeddingCaller | None = None,
    config_loader: Callable[[], ServiceConfig] = load_config,
) -> int:
    """Run the similarity-threshold sweep and print one row per candidate threshold.

    `call_embedding`/`config_loader` are test-only injection seams (no CLI flag exposes
    them, PLAN.md §4's "Dependency injection seam"): left at their defaults, a real
    invocation calls the real, already-configured embedding provider
    (`route_embedding`'s own `default_embedding_caller`) and the real
    `ps_service.config.load_config`. `--dataset` (a real CLI flag) overrides the built-in
    `DEFAULT_DATASET` with a JSON file's worth of pairs; omitted, it sweeps the full
    21-pair `DEFAULT_DATASET`.

    Calls `configure_logging()` before `sweep_dataset()` (PLAN.md §0 F5 -- `route_embedding`
    raises `LoggingLifecycleError` without a configured default emitter). A
    `LlmProviderError` from any `route_embedding` call propagates unchanged out of
    `sweep_dataset()` and out of this function -- no try/except here, mirroring
    `sweep_dataset()`'s own AC-BI-008 guarantee.

    After the sweep, `select_recommendation()` picks the highest-F1 threshold (ties
    broken toward the higher threshold) and `render_recommendation()` renders it as a
    Markdown block, which is printed to stdout (in addition to the per-threshold table)
    and written to `--recommendation-path` (AC-BI-009), creating parent directories as
    needed.

    Returns a process exit code: `0` on success; `1` if `--model` is not given and
    `PS_LLMINTERFACE_EMBED_MODEL` is not resolvable via `config_loader()`.
    """
    args = _parse_args(argv)

    model = _resolve_model(args, config_loader)
    if isinstance(model, int):
        return model

    dataset = DEFAULT_DATASET if args.dataset is None else _load_dataset_from_path(args.dataset)
    thresholds = _threshold_range(args.threshold_start, args.threshold_stop, args.threshold_step)

    configure_logging()

    results = sweep_dataset(dataset, thresholds, model=model, call_embedding=call_embedding)

    _print_threshold_table(results)

    recommendation = select_recommendation(results)
    rendered_recommendation = render_recommendation(recommendation)
    print()
    print(rendered_recommendation)

    recommendation_path = cast("Path", args.recommendation_path)
    recommendation_path.parent.mkdir(parents=True, exist_ok=True)
    recommendation_path.write_text(rendered_recommendation, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
