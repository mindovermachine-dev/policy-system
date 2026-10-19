#!/usr/bin/env python3
"""Sweep candidate similarity thresholds for Company Merge's semantic Capability match.

Scores a labeled dataset of Capability-name pairs through the same embedding and cosine
path Company Merge uses in production, tallies precision, recall and F1 at every
candidate threshold, and recommends the best one. Use it to validate or re-tune
`PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` against real embeddings.

How a pair is scored: `text_a` (the incoming side) is always embedded fresh through
`ps_service.llm_interface.embedding.route_embedding`; `text_b` (the existing-candidate
side) reuses the cached `embedding` of the real similarity-corpus node it cites when one
exists, and is otherwise embedded fresh. The two vectors are compared with
`ps_service.company_merge.similarity.cosine_similarity`, never a reimplementation.
Each unique text is embedded at most once per run, however many pairs and thresholds
reference it.

The built-in dataset has 10 true-match pairs (a human-authored name-style paraphrase
paired with a real Capability node's own `name`) and 11 true-non-match pairs (two real,
distinct Capability nodes' `name` fields). Every pair cites the real GDPR, NIS2 or CRA
Capability node id(s) it is drawn from and carries a non-empty rationale. When the
dataset is built, it is checked against every Capability node in
`test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json`: a pair whose two sides are the same
string and whose string names two or more real nodes is rejected with
`SweepDatasetValidationError`, because Company Merge resolves identical names by exact
key before any semantic scoring happens.

Review band: Company Merge raises a near-miss review for a Capability pair whose best
score is at least `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` (the review floor) and
below `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`; a pair below the floor is dropped
without review. The same scores answer where the floor belongs. The tool prints every
pair's score, then for each candidate floor how many true duplicates the review band
surfaces, how many distinct pairs it surfaces as noise, and how many true duplicates it
hides below the floor. A good floor hides no true duplicate: it is at or below the lowest
true-match score. `--floor X` checks one floor against that rule.

Failure behavior: a `LlmProviderError` from any embedding call propagates unchanged and
aborts the whole sweep; no partial results are produced or written.

Selection: the entry with the highest F1 wins; on equal F1 the higher threshold wins,
which biases the recommendation toward precision (fewer false Capability merges). The
recommendation is printed and written as a short Markdown block.

    uv run tools/company-merge/company_merge_similarity_sweep.py [--model M]

Options:
    --model                 Embedding model for every pair (default: the
                            `llm_interface_embed_model` of the service configuration,
                            i.e. PS_LLMINTERFACE_EMBED_MODEL).
    --threshold-start       First candidate threshold (default: 0.70).
    --threshold-stop        Last candidate threshold, inclusive (default: 0.95).
    --threshold-step        Increment between candidate thresholds (default: 0.01).
    --dataset               JSON file of pair objects (`text_a`, `text_b`, `label` of
                            `match` or `non_match`, `rationale`, optional
                            `cited_node_ids`) replacing the built-in 21-pair dataset.
                            A file dataset is trusted as already curated: the
                            identical-name check runs only for the built-in dataset.
    --merge-threshold       Merge threshold bounding the review band from above
                            (default: 0.59, the chart's capabilityMergeThreshold; the
                            report prints whether the default or the flag was used).
    --floor-start           First candidate review floor (default: 0.30).
    --floor-stop            Last candidate review floor, inclusive (default: the merge
                            threshold).
    --floor-step            Increment between candidate review floors (default: 0.01).
    --floor                 One review floor to check: prints PASS when it is at or below
                            the lowest true-match score and not above the merge
                            threshold, else FAIL, and exits 1 on FAIL.
    --recommendation-path   Where the rendered recommendation Markdown is written
                            (default: the tool's built-in output location); parent
                            directories are created if needed.

Output: a per-threshold table (threshold, precision, recall, F1, TP, FP, TN, FN) and
the recommendation block on stdout, then every pair's score (highest first), the review
band table per floor, the lowest and highest true-match and non-match scores, and the
`--floor` verdict. The recommendation file carries the same recommendation plus a
"Review band" section.

Exit codes:
    0   The sweep completed and the recommendation was written (and `--floor`, if
        given, PASSed).
    1   Neither `--model` nor PS_LLMINTERFACE_EMBED_MODEL resolves to a model, or
        `--floor` was given and FAILed.

Requires a configured embedding provider (its own credentials resolvable by LiteLLM)
unless the embedding transport is injected, and the similarity-corpus baselines checked
out in the repository.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

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

    Attributes:
        text_a: Name-shaped string for the incoming side: a real Capability node's own
            `name` or a human-authored name-style paraphrase, never a description
            sentence, because production only embeds a Capability's `name`. Always
            embedded fresh.
        text_b: Name-shaped string for the existing-candidate side.
        label: `"match"` if the two names denote the same capability, `"non_match"` if
            they are distinct capabilities.
        cited_node_ids: Ids of the real similarity-corpus Capability node(s) the pair is
            drawn from. The last id is the node behind `text_b`.
        rationale: Why the pair carries its label.
        text_b_cached_embedding: The cached `embedding` of the node behind `text_b`,
            reused instead of a fresh embedding call, as production reuses an existing
            canonical node's cached embedding. `None` (the default, and always the case
            for pairs loaded from a `--dataset` file) means `text_b` is embedded fresh.
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
    """Score `pair` through Company Merge's production scoring path.

    `text_a` is always embedded fresh with `model`. `text_b` reuses
    `pair.text_b_cached_embedding` when present and is embedded fresh only when it is
    `None`. The cosine similarity of the two vectors comes from
    `ps_service.company_merge.similarity.cosine_similarity`, not a reimplementation.

    Args:
        pair: The labeled pair to score.
        model: Embedding model identifier passed to `route_embedding`; must be one the
            configured provider serves.
        call_embedding: Embedding transport. `None` lets `route_embedding` use the real
            default caller, so no new endpoint or credential is constructed here.

    Returns:
        The cosine similarity of the two embeddings (at most 1.0, higher means more
        similar).

    Raises:
        LlmProviderError: Propagated unchanged from either embedding call.
        LoggingLifecycleError: Propagated from `route_embedding` if no default log
            emitter has been configured.

    Side Effects:
        Makes up to two embedding calls to the configured provider.
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
    """Report whether two names are the identical string, which scoring must exclude.

    Mirrors Company Merge's own exact-key match (`ps_service.company_merge.dedup`),
    which resolves an identical name before `find_best_semantic_match`'s
    embedding/cosine-similarity path ever runs -- scoring such a pair here would test
    nothing about the semantic threshold being validated.
    """
    return text_a == text_b


class SweepDatasetValidationError(Exception):
    """The built-in dataset contains a same-string pair drawn from a shared name.

    Raised by `build_dataset()` when a pair's `text_a` and `text_b` are the identical
    string and that string is the `name` of two or more real Capability nodes across
    `test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json`. Company Merge would resolve
    such a pair by its exact-key match before semantic scoring runs, so it must never
    reach the dataset.
    """


_TOOLS_COMPANY_MERGE_DIR = Path(__file__).resolve().parent


class CuratedContentCorpusError(Exception):
    """A `test-data/similarity-corpus/{id}/baseline.json` file is missing or fails to parse.

    Raised while loading the curated Capability nodes, which never silently returns an
    empty or partial list.
    """


_CORPUS_DIR = _TOOLS_COMPANY_MERGE_DIR.parent.parent / "test-data" / "similarity-corpus"
_CORPUS_INSTRUMENT_DIRS = ("GDPR-1.0", "NIS2-1.0", "CRA-1.0")


def _load_curated_content_capability_nodes() -> tuple[SerializedNode, ...]:
    """Load every Capability node from the three similarity-corpus baseline exports.

    Reads `test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json` through
    `export.serialize.parse_serialized_graph_json` and keeps the nodes labeled
    `Capability`.

    Raises:
        CuratedContentCorpusError: If any of the three files is missing or does not
            parse as a serialized graph; a failure on any one aborts the whole load.
    """
    nodes: list[SerializedNode] = []
    for instrument_dir in _CORPUS_INSTRUMENT_DIRS:
        path = _CORPUS_DIR / instrument_dir / "baseline.json"
        if not path.is_file():
            raise CuratedContentCorpusError(
                f"{path} does not exist -- build_dataset() requires all three "
                "similarity-corpus baseline.json files"
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
    """Index every real similarity-corpus Capability node by its `id` property.

    `build_dataset()` uses it to look up each spec's `text_b`-side node and attach its
    cached `embedding`. When the same id appears in more than one instrument file (the
    cross-regulation shared-id groups) the last one loaded wins; the entries describe
    the same capability with the same cached embedding in practice.
    """
    return {
        cast("str", node.properties["id"]): node
        for node in _load_curated_content_capability_nodes()
    }


def _text_b_cached_embedding(
    cited_node_ids: tuple[str, ...], nodes_by_id: dict[str, SerializedNode]
) -> tuple[float, ...] | None:
    """The `text_b`-side cited node's own cached `embedding`, if present.

    `cited_node_ids[-1]` is always the id of `text_b`'s node: every match spec's single
    id names it, and every non-match spec lists `text_a`'s node first and `text_b`'s
    second. Returns `None` if the node is missing from `nodes_by_id` or carries no
    `embedding` property, in which case scoring embeds `text_b` fresh.
    """
    node = nodes_by_id.get(cited_node_ids[-1])
    if node is None:
        return None
    embedding = node.properties.get("embedding")
    if embedding is None:
        return None
    return tuple(cast("list[float]", embedding))


def _repeated_capability_names_from_corpus() -> frozenset[str]:
    """Capability `name` values shared by two or more similarity-corpus nodes.

    Counts names across the Capability nodes of
    `test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json`.
    `_assert_dataset_excludes_real_identical_name_pairs` checks the built-in dataset
    against this set, at full similarity-corpus scale.
    """
    names = (
        cast("str", node.properties["name"]) for node in _load_curated_content_capability_nodes()
    )
    counts = Counter(names)
    return frozenset(name for name, count in counts.items() if count >= 2)


def _assert_dataset_excludes_real_identical_name_pairs(
    pairs: tuple[SimilarityPair, ...], repeated_names: frozenset[str]
) -> None:
    """Raise if any pair is a same-string pair drawn from a name shared by real nodes.

    A mechanical check rather than a manual curation discipline: `repeated_names` is
    computed from every real Capability node across similarity-corpus
    (`_repeated_capability_names_from_corpus`), so it catches any such pair whichever
    regulation it came from.
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
            "two or more real corpus nodes as both sides of the same pair -- such pairs "
            "must be excluded, since they resolve via Company Merge's own "
            f"exact-key match before semantic scoring ever runs: {violating_ids!r}"
        )


_CandidateSpec = tuple[str, str, Literal["match", "non_match"], tuple[str, ...], str]

# 10 true-match pairs: each is (human-authored name-style paraphrase, the real node's own
# `name`, "match", the real node's id, a rationale grounded in the real node's own
# `description`). Every name, id and description cited below comes from the real
# `test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json` files.
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

# 11 true-non-match pairs: each cites two real, distinct Capability nodes' own `name`
# fields, with a rationale explaining why the extraction kept them as separate
# capabilities despite shared vocabulary. Every name, id and description cited below
# comes from the real `test-data/similarity-corpus/{GDPR,NIS2,CRA}-1.0/baseline.json` files.
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
        # A genuine hard negative: the two nodes have different descriptions.
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
        # A genuine hard negative: the two nodes have different descriptions.
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
    scored against the same dataset and the same shared embedding cache.

    Attributes:
        threshold: The candidate similarity threshold; a pair is predicted a match when
            its score is at least this value.
        precision: Share of predicted matches that are true matches, 0.0 when none were
            predicted.
        recall: Share of true matches that were predicted, 0.0 when there are none.
        f1: Harmonic mean of precision and recall, 0.0 when both are zero.
        true_positive: Matches predicted as matches.
        false_positive: Non-matches predicted as matches (false merges).
        true_negative: Non-matches predicted as non-matches.
        false_negative: Matches predicted as non-matches (missed merges).
    """

    threshold: float
    precision: float
    recall: float
    f1: float
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int


class ScoredPair(NamedTuple):
    """One labelled pair with the cosine similarity Company Merge would compute for it.

    Attributes:
        pair: The labelled pair.
        score: Cosine similarity of the two names' embeddings (at most 1.0).
    """

    pair: SimilarityPair
    score: float


@dataclass(frozen=True, slots=True)
class FloorResult:
    """What one candidate review floor does to the labelled pairs.

    The review band is `floor <= score < merge_threshold`: pairs in it are raised as
    near-miss reviews, pairs at or above the merge threshold are merged without review,
    and pairs below the floor are silently dropped.

    Attributes:
        floor: The candidate review floor (inclusive).
        merge_threshold: The merge threshold bounding the band from above (exclusive).
        surfaced_true_matches: True duplicates in the band; a reviewer sees them.
        surfaced_distinct_pairs: Distinct (non-match) pairs in the band; review noise.
        hidden_true_matches: True duplicates below the floor; no reviewer sees them.
        matches_missed_by_merge: True duplicates below the merge threshold, whatever the
            floor. Equals `surfaced_true_matches + hidden_true_matches`.
    """

    floor: float
    merge_threshold: float
    surfaced_true_matches: int
    surfaced_distinct_pairs: int
    hidden_true_matches: int
    matches_missed_by_merge: int


def _embed(
    text: str,
    cache: dict[str, tuple[float, ...]],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None,
) -> tuple[float, ...]:
    """Return `text`'s embedding, fetching it via `route_embedding` at most once.

    Checks `cache` first (keyed by the exact `text` string); on a miss, calls
    `route_embedding` and stores the vector before returning it. Callers share one
    `cache`, so a text repeated across pairs or thresholds is fetched once. A
    `LlmProviderError` from `route_embedding` propagates unchanged; nothing is caught
    here, as in Company Merge's own `find_best_semantic_match`.
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
) -> tuple[ScoredPair, ...]:
    """Score every pair once, resolving `text_a` fresh and `text_b` reused-if-cached.

    `text_a` (the "incoming" side) is always resolved through `_embed`, which shares one
    embedding cache local to the call, populated lazily and reused across every pair.
    `text_b` (the "existing candidate" side) reuses `pair.text_b_cached_embedding` when
    present and goes through the same `_embed` cache only when it is `None`. This
    mirrors the always-fresh incoming text and reuse-if-cached existing entry of
    Company Merge's `find_best_semantic_match`.
    """
    cache: dict[str, tuple[float, ...]] = {}
    scored: list[ScoredPair] = []
    for pair in pairs:
        embedding_a = _embed(pair.text_a, cache, model=model, call_embedding=call_embedding)
        if pair.text_b_cached_embedding is not None:
            embedding_b = pair.text_b_cached_embedding
        else:
            embedding_b = _embed(pair.text_b, cache, model=model, call_embedding=call_embedding)
        scored.append(ScoredPair(pair, cosine_similarity(embedding_a, embedding_b)))
    return tuple(scored)


def score_dataset(
    pairs: tuple[SimilarityPair, ...],
    *,
    model: str,
    call_embedding: EmbeddingCaller | None = None,
) -> tuple[ScoredPair, ...]:
    """Score every pair once and return each pair with its cosine similarity.

    Args:
        pairs: The labelled dataset to score.
        model: Embedding model identifier passed to `route_embedding`.
        call_embedding: Embedding transport; `None` uses the real default caller.

    Returns:
        One `ScoredPair` per input pair, in input order.

    Raises:
        LlmProviderError: Propagated unchanged from any embedding call; no partial
            result is returned.
        LoggingLifecycleError: Propagated from `route_embedding` if no default log
            emitter has been configured.

    Side Effects:
        Makes one embedding call per unique text that has no cached vector.
    """
    return _score_dataset(pairs, model=model, call_embedding=call_embedding)


def _threshold_result(scored_pairs: Sequence[ScoredPair], threshold: float) -> ThresholdResult:
    """Tally the confusion matrix, precision, recall and F1 at one threshold."""
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

    Builds one embedding cache for the call, populated lazily as `pairs` are scored and
    reused across every pair and every threshold, so each unique text is embedded at
    most once however many pairs reference it or thresholds are swept.

    Args:
        pairs: The labeled dataset to score.
        thresholds: Candidate thresholds, in the order results should be returned. A
            pair is predicted a match when its score is at least the threshold.
        model: Embedding model identifier passed to `route_embedding`.
        call_embedding: Embedding transport; `None` uses the real default caller.

    Returns:
        One `ThresholdResult` per threshold, in input order; empty if `thresholds` is
        empty.

    Raises:
        LlmProviderError: Propagated unchanged from any embedding call. Every pair is
            scored before any result is produced, so a failure partway aborts the
            sweep and no partial list is returned.
        LoggingLifecycleError: Propagated from `route_embedding` if no default log
            emitter has been configured.

    Side Effects:
        Makes one embedding call per unique text that has no cached vector.
    """
    scored_pairs = score_dataset(pairs, model=model, call_embedding=call_embedding)
    return [_threshold_result(scored_pairs, threshold) for threshold in thresholds]


def review_band_report(
    scored: Sequence[ScoredPair], floors: Iterable[float], merge_threshold: float
) -> list[FloorResult]:
    """Count, per candidate review floor, the pairs the review band surfaces and hides.

    A pair is in the band when `floor <= score < merge_threshold`: the floor is
    inclusive (as `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` is in Company Merge) and
    the merge threshold exclusive (a score at the merge threshold merges instead).

    Args:
        scored: Scored labelled pairs.
        floors: Candidate review floors, in the order rows should be returned.
        merge_threshold: The merge threshold bounding the band from above.

    Returns:
        One `FloorResult` per floor, in input order; empty if `floors` is empty.
    """
    missed = sum(
        1 for item in scored if item.pair.label == "match" and item.score < merge_threshold
    )
    results: list[FloorResult] = []
    for floor in floors:
        surfaced_matches = 0
        surfaced_distinct = 0
        hidden_matches = 0
        for item in scored:
            in_band = floor <= item.score < merge_threshold
            if item.pair.label == "match":
                if in_band:
                    surfaced_matches += 1
                elif item.score < floor:
                    hidden_matches += 1
            elif in_band:
                surfaced_distinct += 1
        results.append(
            FloorResult(
                floor=floor,
                merge_threshold=merge_threshold,
                surfaced_true_matches=surfaced_matches,
                surfaced_distinct_pairs=surfaced_distinct,
                hidden_true_matches=hidden_matches,
                matches_missed_by_merge=missed,
            )
        )
    return results


def _label_scores(scored: Sequence[ScoredPair], label: str) -> list[float]:
    return [item.score for item in scored if item.pair.label == label]


def floor_passes(floor: float, lowest_true_match: float | None, merge_threshold: float) -> bool:
    """Report whether `floor` keeps every labelled true duplicate visible.

    A floor passes when it is at or below the lowest true-match score (so none is hidden)
    and does not exceed the merge threshold (Company Merge rejects a review floor above
    the merge threshold). With no true match in the dataset only the second condition
    applies.
    """
    if floor > merge_threshold:
        return False
    return lowest_true_match is None or floor <= lowest_true_match


def build_dataset() -> tuple[SimilarityPair, ...]:
    """Build the full labeled sweep dataset from the similarity-corpus baselines.

    Returns:
        10 true-match pairs followed by 11 true-non-match pairs. Each cites the real
        GDPR, NIS2 or CRA Capability node id(s) it is drawn from and has a non-empty
        rationale. Each pair's `text_b_cached_embedding` is the cached `embedding` of
        the node behind `text_b` (`cited_node_ids[-1]`), or `None` if that node has
        none.

    Raises:
        CuratedContentCorpusError: If a similarity-corpus `baseline.json` is missing or
            malformed.
        SweepDatasetValidationError: If a pair uses a name shared by two or more real
            nodes on both of its sides. The dataset is checked mechanically against the
            whole similarity-corpus corpus rather than trusted to hand curation.

    Side Effects:
        Reads the three similarity-corpus `baseline.json` files.
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


DEFAULT_THRESHOLD_START = 0.70
DEFAULT_THRESHOLD_STOP = 0.95
DEFAULT_THRESHOLD_STEP = 0.01

# Mirrors `psService.companyMerge.capabilityMergeThreshold` in
# charts/policy-system/values.yaml; a hermetic test fails when the two drift apart.
DEFAULT_MERGE_THRESHOLD = 0.59
DEFAULT_FLOOR_START = 0.30
DEFAULT_FLOOR_STEP = 0.01

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
    """A `--dataset` file could not be parsed into `SimilarityPair`s (invalid `label`)."""


def _load_dataset_from_path(path: Path) -> tuple[SimilarityPair, ...]:
    """Load a `--dataset` override file: a JSON array of `SimilarityPair`-shaped objects.

    Each entry requires `text_a`, `text_b`, `label` (`"match"`/`"non_match"`) and
    `rationale`; `cited_node_ids` (a list of strings) is optional. Tests use this to
    substitute a small, hermetic fixture for the built-in dataset. The identical-name
    exclusion check runs only in `build_dataset()`, so a file dataset is trusted as
    already curated. Raises `SweepDatasetFileError` for an invalid `label` and
    `KeyError` for a missing required field; `OSError` and `json.JSONDecodeError`
    propagate if the file cannot be read or is not JSON.
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
        "21-pair dataset",
    )
    parser.add_argument(
        "--merge-threshold",
        type=float,
        default=None,
        help="Merge threshold bounding the review band from above (exclusive). Default: "
        f"{DEFAULT_MERGE_THRESHOLD}, the chart's capabilityMergeThreshold; the report "
        "prints which source was used",
    )
    parser.add_argument(
        "--floor-start",
        type=float,
        default=DEFAULT_FLOOR_START,
        help=f"First candidate review floor to report (default: {DEFAULT_FLOOR_START})",
    )
    parser.add_argument(
        "--floor-stop",
        type=float,
        default=None,
        help="Last candidate review floor to report, inclusive (default: the merge threshold)",
    )
    parser.add_argument(
        "--floor-step",
        type=float,
        default=DEFAULT_FLOOR_STEP,
        help=f"Increment between candidate review floors (default: {DEFAULT_FLOOR_STEP})",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=None,
        help="Check one review floor: print PASS when it is at or below the lowest "
        "true-match score and not above the merge threshold, else FAIL and exit 1",
    )
    parser.add_argument(
        "--recommendation-path",
        type=Path,
        default=_DEFAULT_RECOMMENDATION_PATH,
        help="Where the rendered recommendation Markdown is written (default: the "
        "tool's built-in output location); parent directories are created if needed",
    )
    return parser.parse_args(argv)


def _resolve_model(
    args: argparse.Namespace, config_loader: Callable[[], ServiceConfig]
) -> str | int:
    """Fail-closed `--model` resolution. An `int` return is the process exit code.

    An explicit `--model` always wins; otherwise `config_loader()` (the real
    `load_config` by default) must resolve `llm_interface_embed_model`, the model
    Company Merge uses in production, or a clear message goes to stderr and `1` is
    returned instead of silently falling back to some other value.
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


_FLOOR_HEADER = "floor  surfaced_matches  surfaced_distinct  hidden_matches  missed_by_merge"


def _format_score(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.4f}"


def _extremes(scored: Sequence[ScoredPair]) -> list[tuple[str, float | None]]:
    """Lowest and highest true-match and non-match scores, `None` when a label is absent."""
    matches = _label_scores(scored, "match")
    non_matches = _label_scores(scored, "non_match")
    return [
        ("lowest true-match score", min(matches, default=None)),
        ("highest true-match score", max(matches, default=None)),
        ("lowest non-match score", min(non_matches, default=None)),
        ("highest non-match score", max(non_matches, default=None)),
    ]


def _print_pair_table(scored: Sequence[ScoredPair]) -> None:
    """Print one row per scored pair, highest score first: score, label, both names."""
    print("score   label      text_a | text_b")
    for item in sorted(scored, key=lambda scored_pair: scored_pair.score, reverse=True):
        print(f"{item.score:<8.4f}{item.pair.label:<11}{item.pair.text_a} | {item.pair.text_b}")


def _print_floor_table(rows: Sequence[FloorResult]) -> None:
    """Print one row per candidate review floor with the review-band counts."""
    print(_FLOOR_HEADER)
    for row in rows:
        print(
            f"{row.floor:<7.2f}"
            f"{row.surfaced_true_matches:<18d}"
            f"{row.surfaced_distinct_pairs:<19d}"
            f"{row.hidden_true_matches:<16d}"
            f"{row.matches_missed_by_merge:<d}"
        )


def render_review_band(
    rows: Sequence[FloorResult],
    scored: Sequence[ScoredPair],
    *,
    merge_threshold: float,
    merge_source: str,
    floor_verdict: str | None = None,
) -> str:
    """Render the review-band report as a Markdown section.

    Args:
        rows: One `FloorResult` per candidate floor.
        scored: The scored pairs, for the true-match score extremes.
        merge_threshold: The merge threshold bounding the band.
        merge_source: Where the merge threshold came from (`default` or
            `--merge-threshold`).
        floor_verdict: The `--floor` PASS/FAIL line, if a floor was checked.

    Returns:
        Markdown starting with a `## Review band` heading: the merge threshold and its
        source, a table with one row per floor, the true-match score extremes, and the
        verdict line when given.
    """
    lines = [
        "## Review band",
        "",
        "A pair is raised for review when `floor <= score < merge threshold`.",
        "",
        f"- merge threshold {merge_threshold:g} (source: {merge_source})",
    ]
    matches = _label_scores(scored, "match")
    lines.append(f"- Lowest true-match score: {_format_score(min(matches, default=None))}")
    lines.append(f"- Highest true-match score: {_format_score(max(matches, default=None))}")
    missed = rows[0].matches_missed_by_merge if rows else 0
    lines.append(f"- True duplicates below the merge threshold: {missed}")
    if floor_verdict is not None:
        lines.append(f"- {floor_verdict}")
    lines += [
        "",
        "| floor | surfaced true matches | surfaced distinct pairs | hidden true matches |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {row.floor:.2f} | {row.surfaced_true_matches} | {row.surfaced_distinct_pairs} "
        f"| {row.hidden_true_matches} |"
        for row in rows
    ]
    return "\n".join(lines) + "\n"


def select_recommendation(results: list[ThresholdResult]) -> ThresholdResult:
    """Pick the recommended threshold from a full sweep's results.

    Selection rule: the entry with the highest `f1`. On equal `f1` the higher
    `threshold` wins, which deliberately biases the recommendation toward precision
    (fewer false merges) in line with the domain's anti-over-merging preference. This
    does not depend on iteration order: `max()` keys on the `(f1, threshold)` tuple.

    Args:
        results: The `ThresholdResult` list of a full sweep, in any order.

    Returns:
        The entry with the highest F1, the higher threshold winning a tie.

    Raises:
        ValueError: If `results` is empty, since there is nothing to recommend.
    """
    if not results:
        raise ValueError("select_recommendation requires at least one ThresholdResult")
    return max(results, key=lambda result: (result.f1, result.threshold))


def render_recommendation(result: ThresholdResult, review_band: str | None = None) -> str:
    """Render `result` as a short Markdown block naming the recommended threshold.

    Args:
        result: The chosen `ThresholdResult`.
        review_band: Markdown from `render_review_band`, appended after a blank line;
            `None` leaves the block without a review-band section.

    Returns:
        Markdown text naming the threshold and its precision, recall and F1 (each to 3
        decimal places), the TP/FP/TN/FN counts, and the selection rule (highest F1,
        ties toward the higher threshold), so a reader of the written recommendation
        file sees exactly what was measured and why the value was chosen.
    """
    block = (
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
    return block if review_band is None else f"{block}\n{review_band}"


def main(
    argv: Sequence[str] | None = None,
    *,
    call_embedding: EmbeddingCaller | None = None,
    config_loader: Callable[[], ServiceConfig] = load_config,
) -> int:
    """Run the similarity-threshold sweep, print the table and write the recommendation.

    Configures logging before sweeping, because `route_embedding` raises
    `LoggingLifecycleError` without a default emitter. After the sweep it picks the
    highest-F1 threshold (ties toward the higher one), prints the per-threshold table
    and the rendered Markdown block, and writes the block to `--recommendation-path`,
    creating parent directories.

    Args:
        argv: Command-line arguments (see `--help`); `None` reads `sys.argv`.
        call_embedding: Embedding transport. Test-only seam (no CLI flag exposes it);
            `None` uses the real default caller and the configured provider.
        config_loader: Loads the service configuration used to resolve the default
            `--model`. Test-only seam; the default is `ps_service.config.load_config`.

    Returns:
        Process exit code: `0` on success; `1` if `--model` is not given and
        `PS_LLMINTERFACE_EMBED_MODEL` does not resolve through `config_loader()`, or if
        `--floor` was given and FAILed (the full report and file are still produced).

    Raises:
        SystemExit: Raised by argparse on invalid arguments or `--help`.
        SweepDatasetFileError: If a `--dataset` file has a pair with an invalid label.
        CuratedContentCorpusError: Without `--dataset`, if a similarity-corpus
            `baseline.json` is missing or malformed.
        SweepDatasetValidationError: Without `--dataset`, if the built-in dataset
            uses a name shared by real nodes on both sides of a pair.
        LlmProviderError: Propagated unchanged from any embedding call; nothing is
            written in that case.
        ValueError: If `--threshold-step` or `--floor-step` is not positive.
        OSError: If a `--dataset` file cannot be read or the recommendation file
            cannot be written.

    Side Effects:
        Without `--dataset`, reads the three similarity-corpus `baseline.json` files.
        Configures logging, calls the embedding provider, prints to stdout (or the
        failed model resolution to stderr) and writes the recommendation file.
    """
    args = _parse_args(argv)

    model = _resolve_model(args, config_loader)
    if isinstance(model, int):
        return model

    dataset = build_dataset() if args.dataset is None else _load_dataset_from_path(args.dataset)
    thresholds = _threshold_range(args.threshold_start, args.threshold_stop, args.threshold_step)
    explicit_merge = cast("float | None", args.merge_threshold)
    merge_threshold = DEFAULT_MERGE_THRESHOLD if explicit_merge is None else explicit_merge
    merge_source = "default" if explicit_merge is None else "--merge-threshold"
    floor_stop = merge_threshold if args.floor_stop is None else cast("float", args.floor_stop)
    floors = _threshold_range(args.floor_start, floor_stop, args.floor_step)

    configure_logging()

    scored = score_dataset(dataset, model=model, call_embedding=call_embedding)
    results = [_threshold_result(scored, threshold) for threshold in thresholds]

    _print_threshold_table(results)

    recommendation = select_recommendation(results)
    print()
    print(render_recommendation(recommendation))

    rows = review_band_report(scored, floors, merge_threshold)
    lowest_match = min(_label_scores(scored, "match"), default=None)
    floor_verdict: str | None = None
    passed = True
    if args.floor is not None:
        passed = floor_passes(cast("float", args.floor), lowest_match, merge_threshold)
        floor_verdict = (
            f"floor {args.floor:g}: {'PASS' if passed else 'FAIL'} "
            f"(lowest true-match score {_format_score(lowest_match)}, "
            f"merge threshold {merge_threshold:g})"
        )

    print(f"merge threshold {merge_threshold:g} (source: {merge_source})")
    print()
    _print_pair_table(scored)
    print()
    _print_floor_table(rows)
    print()
    for label, value in _extremes(scored):
        print(f"{label}: {_format_score(value)}")
    if floor_verdict is not None:
        print()
        print(floor_verdict)

    review_band = render_review_band(
        rows,
        scored,
        merge_threshold=merge_threshold,
        merge_source=merge_source,
        floor_verdict=floor_verdict,
    )
    recommendation_path = cast("Path", args.recommendation_path)
    recommendation_path.parent.mkdir(parents=True, exist_ok=True)
    recommendation_path.write_text(
        render_recommendation(recommendation, review_band), encoding="utf-8"
    )

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
