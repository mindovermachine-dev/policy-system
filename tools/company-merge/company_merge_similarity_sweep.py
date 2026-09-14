#!/usr/bin/env python3
r"""Maintainer script: empirically validate Company Merge's semantic-match threshold (issue #29).

**Increment 1 of issue #29** (PLAN.md §1 Increment 1, CHANGES.md row M3's path
substitution to `tools/company-merge/` and row m2's AST import-scan test): the thinnest
possible real slice -- a labeled `SimilarityPair` shape, `score_pair()` scoring one pair
through the *exact* production path Company Merge itself uses
(`ps_service.llm_interface.embedding.route_embedding` for each side,
`ps_service.company_merge.similarity.cosine_similarity` directly on the two resulting
vectors -- never a reimplementation, AC-BI-004/AC-BI-005/AC-BI-006), and a 3-candidate
`build_dataset()` that mechanically proves the AC-BI-003 identical-Capability-name
exclusion rule rather than merely omitting such a pair by hand.

**Increment 2** (PLAN.md §1 Increment 2, AC-BI-007): `sweep_dataset()` scores a whole
dataset against a whole range of candidate thresholds while fetching each unique text's
embedding at most once, via a single `dict[str, tuple[float, ...]]` cache
(`_embed()`) shared across every pair and every threshold.

**Increment 3** (PLAN.md §1 Increment 3, AC-BI-008): no `try`/`except` anywhere in
`_embed`/`sweep_dataset` -- a `LlmProviderError` from any `route_embedding` call
propagates unchanged, aborting the whole sweep with no partial `ThresholdResult` list,
mirroring `ps_service.company_merge.dedup.find_best_semantic_match`'s own documented
"no try/except in this function" (`dedup.py:159-163`) and `dedupe_canonical_nodes`'s
call-order-based "abort with no partial write" guarantee (`dedup.py:229-232`).

**Increment 4** (PLAN.md §1 Increment 4, CHANGES.md row M3's path substitution):
`build_dataset()` now returns the full labeled dataset -- 10 true-match pairs (PLAN.md
§3.2, human-authored name-style paraphrase grounded in a real node's own `description`
paired with that node's real `name`) and 11 true-non-match pairs (PLAN.md §3.1, two
real, distinct Capability nodes' `name` fields verbatim) -- every pair citing the real
GDPR/NIS2/CRA Capability node id(s) it is drawn from (AC-BI-001) with a non-empty
`rationale` (AC-BI-002). `build_dataset()` also runs a real mechanical exclusion guard
(AC-BI-003's general form, not just a handful of illustrative examples): it loads every
real Capability node from all three `test-data/eu-regulations/*.json` files at
construction time, computes the set of `name` values shared by two or more of the 90
real nodes, and raises `SweepDatasetValidationError` if any dataset pair's `text_a` and
`text_b` are identical to each other AND that shared string names two or more real
corpus nodes -- proven against the whole 90-node corpus, not merely asserted by
convention.

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
from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from ps_service.config import ServiceConfig
    from ps_service.llm_interface.client import EmbeddingCaller


@dataclass(frozen=True, slots=True)
class SimilarityPair:
    """One labeled Capability-name pair for the similarity-threshold sweep dataset.

    `text_a`/`text_b` are name-shaped strings -- either a real Capability node's own
    `name` field, verbatim, or a human-authored name-style paraphrase -- never a
    description sentence, because `route_embedding` is only ever called on a
    Capability's `name` in production (`ps_service.company_merge.dedup`'s
    `_TEXT_PROPERTY_BY_LABEL`).
    """

    text_a: str
    text_b: str
    label: Literal["match", "non_match"]
    cited_node_ids: tuple[str, ...]
    rationale: str


def score_pair(
    pair: SimilarityPair,
    *,
    model: str,
    call_embedding: EmbeddingCaller | None = None,
) -> float:
    """Score `pair` through Company Merge's real production scoring path.

    Calls `route_embedding` once for `text_a` and once for `text_b` with the given
    `model` (AC-BI-005 -- "the same model config Company Merge uses in production"),
    then calls `ps_service.company_merge.similarity.cosine_similarity` directly on the
    two resulting vectors (AC-BI-004 -- never a reimplementation). `call_embedding`
    defaults to `None`, which `route_embedding` itself resolves to the real
    `default_embedding_caller` (AC-BI-006 -- no new endpoint/credential/dependency is
    ever constructed here). A `LlmProviderError` from either `route_embedding` call
    propagates unchanged -- no try/except in this function.
    """
    embedding_a = route_embedding(pair.text_a, model=model, call_embedding=call_embedding)
    embedding_b = route_embedding(pair.text_b, model=model, call_embedding=call_embedding)
    return cosine_similarity(tuple(embedding_a.vector), tuple(embedding_b.vector))


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
    `test-data/eu-regulations/{gdpr,nis2,cra}.json` -- such a pair would resolve via
    Company Merge's own exact-key match before semantic scoring ever runs, so it must
    never reach the shipped dataset (PLAN.md §1 Increment 4, §3.3).
    """


_TOOLS_COMPANY_MERGE_DIR = Path(__file__).resolve().parent
_TEST_DATA_EU_REGULATIONS_DIR = (
    _TOOLS_COMPANY_MERGE_DIR.parent.parent / "test-data" / "eu-regulations"
)
_CAPABILITY_SOURCE_FILENAMES = ("gdpr.json", "nis2.json", "cra.json")


def _repeated_capability_names_from_corpus() -> frozenset[str]:
    """Load every real Capability node from all three EU-regulation fixture files.

    Returns the set of `name` values shared by two or more of the 90 real Capability
    nodes across `gdpr.json`/`nis2.json`/`cra.json` (PLAN.md §3.3: 19 such names,
    confirmed by direct count) -- the general-form input `_assert_dataset_excludes_
    real_identical_name_pairs` checks the shipped dataset against, in place of the 3-5
    illustrative examples Increment 1 used.
    """
    names: list[str] = []
    for filename in _CAPABILITY_SOURCE_FILENAMES:
        graph = json.loads((_TEST_DATA_EU_REGULATIONS_DIR / filename).read_text(encoding="utf-8"))
        names.extend(
            node["properties"]["name"]
            for node in graph["nodes"]
            if node.get("label") == "Capability"
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
    computed from all 90 real Capability nodes (`_repeated_capability_names_from_
    corpus`), so this catches any such pair regardless of which regulation(s) it came
    from, not just the handful of names cited by name in PLAN.md's prose.
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

# 10 true-match pairs (PLAN.md §3.2): each is (human-authored name-style paraphrase,
# the real node's own `name`, "match", the real node's id, a rationale grounded in the
# real node's own `description`). Every real name/id/description cited below was
# independently re-verified against the live `test-data/eu-regulations/*.json` files
# for this dispatch (not copied blindly from PLAN.md's prose) -- all 10 matched exactly,
# no corrections were needed.
_MATCH_PAIR_SPECS: tuple[_CandidateSpec, ...] = (
    (
        "Identity Verification and Unauthorised Access Prevention",
        "Access Control & Authentication",
        "match",
        ("cap_access_control_authentication_151816",),
        (
            "Paraphrase grounded in the real node's own description: 'prevent unauthorised "
            "access via authentication and identity/access management, and to report on "
            "unauthorised access attempts.' Never verbatim-identical to the real name, so "
            "AC-BI-003 does not exclude it."
        ),
    ),
    (
        "Cryptographic Protection of Data at Rest and In Transit",
        "Data Encryption",
        "match",
        ("cap_data_encryption_0e50d3",),
        (
            "Paraphrase grounded in the real node's own description: 'protect the "
            "confidentiality of stored, transmitted or processed data using "
            "state-of-the-art encryption.'"
        ),
    ),
    (
        "Cybersecurity Incident Detection, Response and Containment",
        "Incident Handling",
        "match",
        ("cap_incident_handling_4cf73e",),
        (
            "Paraphrase grounded in the real node's own description: 'detect, respond to "
            "and contain cybersecurity incidents affecting network and information "
            "systems.'"
        ),
    ),
    (
        "Ongoing Identification and Remediation of Product Vulnerabilities",
        "Vulnerability Management",
        "match",
        ("cap_vulnerability_management_55d0c4",),
        (
            "Paraphrase grounded in the real node's own description: 'identify, triage, "
            "and remediate vulnerabilities in a product and its components on an ongoing "
            "basis.'"
        ),
    ),
    (
        "Privacy Risk Assessment for High-Risk Data Processing",
        "Data Protection Impact Assessment",
        "match",
        ("cap_data_protection_impact_assessment_a51acb",),
        (
            "Paraphrase grounded in the real node's own description: 'assess, document "
            "and keep current the impact of high-risk processing operations on the "
            "protection of personal data.'"
        ),
    ),
    (
        "Security-by-Design Product Development Process",
        "Secure Development Lifecycle",
        "match",
        ("cap_secure_development_lifecycle_9f3224",),
        (
            "Paraphrase grounded in the real node's own description: 'design, develop and "
            "produce products so that they achieve an appropriate, risk-based level of "
            "cybersecurity by default.'"
        ),
    ),
    (
        "Backup, Disaster Recovery and Crisis Management Capacity",
        "Business Continuity & Disaster Recovery",
        "match",
        ("cap_business_continuity_disaster_recovery_9c1c32",),
        (
            "Paraphrase grounded in the real node's own description: 'maintain backup "
            "management, disaster recovery and crisis management processes that sustain "
            "operations through an incident.'"
        ),
    ),
    (
        "Data Subject Consent Capture, Recording and Withdrawal",
        "Consent Management",
        "match",
        ("cap_consent_management_590926",),
        (
            "Paraphrase grounded in the real node's own description: 'obtain, present, "
            "record, demonstrate and enable withdrawal of data subject consent.'"
        ),
    ),
    (
        "Documented Policy for Coordinated Disclosure of Vulnerabilities",
        "Coordinated Vulnerability Disclosure Policy",
        "match",
        ("cap_coordinated_vulnerability_disclosure_policy_c23221",),
        (
            "Paraphrase grounded in the real node's own description: 'maintain and "
            "enforce a documented policy governing coordinated vulnerability "
            "disclosure.'"
        ),
    ),
    (
        "Personnel Security and Organisational Asset Inventory Control",
        "Asset & Personnel Security Management",
        "match",
        ("cap_asset_personnel_security_management_e68e9a",),
        (
            "Paraphrase grounded in the real node's own description: 'manage human "
            "resources security and maintain an inventory and control regime over "
            "organisational assets.'"
        ),
    ),
)

# 11 true-non-match pairs (PLAN.md §3.1): each cites two real, distinct Capability
# nodes' own `name` fields verbatim, with a rationale explaining why the extraction
# kept them as separate capabilities despite shared vocabulary. Every real name/id/
# description cited below was independently re-verified against the live
# `test-data/eu-regulations/*.json` files for this dispatch -- all 11 matched exactly,
# no corrections were needed.
_NON_MATCH_PAIR_SPECS: tuple[_CandidateSpec, ...] = (
    (
        "Supply Chain Security Management",
        "Supply-Chain Traceability Records",
        "non_match",
        (
            "cap_supply_chain_security_management_d1f111",
            "cap_supply_chain_traceability_records_fd07d2",
        ),
        (
            "Both are 'supply chain'-named and cross-regulation (NIS2 vs CRA), but one is "
            "proactive security risk management of suppliers and the other is passive "
            "identity record-keeping -- distinct capabilities despite the shared vocabulary."
        ),
    ),
    (
        "Exploitation Mitigation",
        "Incident Handling",
        "non_match",
        ("cap_exploitation_mitigation_07f70d", "cap_incident_handling_4cf73e"),
        (
            "Both incident-adjacent (CRA vs GDPR/NIS2), but Exploitation Mitigation is a "
            "product-level technical mitigation capacity baked into the product itself, "
            "while Incident Handling is an organisational detect/respond/contain process."
        ),
    ),
    (
        "Secure Default Configuration",
        "Data Protection by Design & Default",
        "non_match",
        (
            "cap_secure_default_configuration_3bfba6",
            "cap_data_protection_by_design_default_69e489",
        ),
        (
            "Both use 'by default'/'by design' language across regulations (CRA vs "
            "GDPR), but one is a product-configuration capacity and the other a broader "
            "legal/organisational principle-embedding capacity -- a plausible "
            "false-positive risk for an under-tuned threshold."
        ),
    ),
    (
        "Business Continuity Notification",
        "Business Continuity & Disaster Recovery",
        "non_match",
        (
            "cap_business_continuity_notification_d67b9c",
            "cap_business_continuity_disaster_recovery_9c1c32",
        ),
        (
            "Both names literally share 'Business Continuity' (CRA vs GDPR/NIS2), but "
            "one is a notification duty and the other an operational-resilience "
            "capacity -- a strong lexical-overlap hard negative."
        ),
    ),
    (
        "Data Integrity & Confidentiality Assurance",
        "Data & Configuration Integrity Protection",
        "non_match",
        (
            "cap_data_integrity_confidentiality_assurance_d9586b",
            "cap_data_configuration_integrity_protection_882f84",
        ),
        (
            "Both live in GDPR's own extraction as separate nodes despite overlapping "
            "'integrity' language -- proves the extraction already treats these as "
            "distinct, so the sweep must not recommend a threshold that would merge "
            "them."
        ),
    ),
    (
        "Processor Due Diligence & Selection",
        "Processor Contract Management",
        "non_match",
        (
            "cap_processor_due_diligence_selection_e36605",
            "cap_processor_contract_management_4ef8b2",
        ),
        (
            "Adjacent stages of the same processor-governance lifecycle (selection vs "
            "contracting, both GDPR), kept as distinct capabilities."
        ),
    ),
    (
        "International Data Transfer Governance",
        "Binding Corporate Rules Governance",
        "non_match",
        (
            "cap_international_data_transfer_governance_e6c9c5",
            "cap_binding_corporate_rules_governance_5d8a7a",
        ),
        (
            "Binding Corporate Rules is one specific mechanism within the broader "
            "international-transfer governance capacity (both GDPR) -- the extraction "
            "kept the general capacity and the specific-mechanism capacity separate."
        ),
    ),
    (
        "Cybersecurity Training & Awareness",
        "Data Protection Advisory & Awareness",
        "non_match",
        (
            "cap_cybersecurity_training_awareness_63b70e",
            "cap_data_protection_advisory_awareness_7171df",
        ),
        (
            "Both 'awareness'-named across regulations (NIS2 vs GDPR), but one is "
            "security-hygiene training and the other is legal/privacy advisory -- "
            "different capacities."
        ),
    ),
    (
        "Component Inventory & SBOM Management",
        "Supply Chain Security Management",
        "non_match",
        (
            "cap_component_inventory_sbom_management_b5223c",
            "cap_supply_chain_security_management_d1f111",
        ),
        (
            "SBOM/component inventory is commonly discussed alongside supply-chain "
            "security in practice (CRA vs NIS2), but the extraction scoped them as "
            "distinct capabilities (documentation artifact vs relationship/risk "
            "management)."
        ),
    ),
    (
        "Automated Decision-Making Safeguards",
        "Cybersecurity Risk Assessment Process",
        "non_match",
        (
            "cap_automated_decision_making_safeguards_dc9ed9",
            "cap_cybersecurity_risk_assessment_process_ce1a2f",
        ),
        (
            "An unambiguous negative anchor (GDPR vs NIS2) -- different regulatory "
            "domains, near-zero expected similarity -- confirms the sweep isn't "
            "inflating recall by only testing hard cases."
        ),
    ),
    (
        "Regulatory Registration & Information Management",
        "Compliance Documentation Management",
        "non_match",
        (
            "cap_regulatory_registration_information_management_98b76b",
            "cap_compliance_documentation_management_a87281",
        ),
        (
            "Both are 'provide information to authorities' capacities (NIS2 vs "
            "GDPR/CRA), but one is entity registration data and the other is technical "
            "conformity documentation -- a second hard negative testing "
            "authority-facing-duty confusion."
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
    behavior (`ps_service.company_merge.dedup`, `dedup.py:159-163`).
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
    """Score every pair once, resolving both sides through one shared embedding cache.

    The cache is local to a single `sweep_dataset()` call, populated lazily as pairs
    are scored, and reused across every pair -- never a direct `route_embedding` call
    from this loop; every embedding is resolved through `_embed`.
    """
    cache: dict[str, tuple[float, ...]] = {}
    scored: list[tuple[SimilarityPair, float]] = []
    for pair in pairs:
        embedding_a = _embed(pair.text_a, cache, model=model, call_embedding=call_embedding)
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
    whole 90-node corpus (`_assert_dataset_excludes_real_identical_name_pairs`,
    construction-time, raising `SweepDatasetValidationError` if violated) rather than
    relying on the dataset having been curated by hand correctly -- see that
    function's docstring.
    """
    pairs = tuple(
        SimilarityPair(
            text_a=text_a,
            text_b=text_b,
            label=label,
            cited_node_ids=cited_node_ids,
            rationale=rationale,
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
