"""Content lint for `ps-graph-cleanup/SKILL.md` and its registrations (issue #190).

The skill is created in slice 6 (candidate discovery) and grows one step per
slice; this file grows with it. Pins what the skill promises about the tools
that actually exist, the gate error texts, and the plugin/readme registration.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "ps-skills" / "ps-plugin"
_SKILL = _PLUGIN / "skills" / "ps-graph-cleanup" / "SKILL.md"


def _text() -> str:
    return " ".join(_SKILL.read_text(encoding="utf-8").split())


def test_skill_has_frontmatter_name_and_description() -> None:
    raw = _SKILL.read_text(encoding="utf-8")

    assert raw.startswith("---\nname: ps-graph-cleanup\ndescription: ")


def test_skill_names_the_connector_and_the_discovery_tool() -> None:
    text = _text()

    assert "ps-mcp" in text
    assert "find-capability-merge-candidates" in text


def test_skill_states_the_authorization_contract() -> None:
    text = _text()

    assert "ComplianceOfficer" in text
    assert "no admin override" in text.lower()
    assert "local-test bypass" in text.lower()


def test_skill_reports_the_gate_error_texts_verbatim() -> None:
    text = _text()

    assert "error: You do not have the required access role for this action." in text
    assert "error: graph cleanup requires a real authenticated caller" in text
    assert "error: The authorization store is temporarily unavailable." in text
    assert "error: the policy graph database is not reachable" in text


def test_skill_says_discovery_is_read_only_and_a_human_decides() -> None:
    text = _text().lower()

    assert "read-only" in text
    assert "never merge" in text


def test_readme_and_plugin_manifest_register_the_skill() -> None:
    readme = (_REPO / "ps-skills" / "readme.md").read_text(encoding="utf-8")
    manifest = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert "ps-graph-cleanup" in readme
    assert "graph cleanup" in manifest["description"].lower()


def test_skill_documents_embedding_similarity_and_its_threshold() -> None:
    text = _text()

    assert "min_similarity" in text
    assert "0.90" in text
    assert "`embedding`" in text
    assert "cached embedding" in text


def test_skill_documents_governance_obligation_count_and_merge_case() -> None:
    text = _text()

    assert "obligation_count" in text
    assert "governing_policy" in text
    assert "merge_case" in text
    assert "policies_distinct" in text
    for case in ("| 1 |", "| 2 |", "| 3 |"):
        assert case in text


def test_skill_documents_duplicate_obligation_discovery() -> None:
    text = _text()

    assert "find-duplicate-obligations" in text
    assert "role_id" in text
    assert "same role" in text.lower()
    assert "source_ref" in text
    assert "near_text" in text
    assert "identical_text" in text


def test_skill_walks_the_capability_merge_flow_in_order() -> None:
    text = _text()

    assert "merge-capabilities" in text
    assert "check-cleanup-approval" in text
    positions = [
        text.index(marker)
        for marker in (
            "**Candidate.**",
            "**Preview.**",
            "**Confirm.**",
            "**Passkey.**",
            "**Result.**",
        )
    ]
    assert positions == sorted(positions)


def test_skill_states_the_merge_is_signed_by_the_officer_and_never_automatic() -> None:
    text = _text().lower()

    assert "approval_url" in text
    assert "passkey" in text
    assert "never open, sign or complete it on the officer's behalf" in text
    assert "never merge a pair the compliance officer has not explicitly confirmed" in text


def test_skill_documents_the_named_merge_errors() -> None:
    text = _text()

    assert "error: a capability cannot be merged into itself" in text
    assert "merged tombstone" in text
    assert "`applied` audit row followed by a `failed` row" in text


def test_skill_documents_case_two_and_the_acknowledgment_gate() -> None:
    text = _text()
    section = text[text.index("### Merging two capabilities") :]

    assert "acknowledge_governance_change" in section
    assert "acknowledgment_required" in section
    assert "no approval" in section.lower()
    assert "its governed set changes, its content/version does not" in section
    assert "obligations_coverage_changed" in section
    assert "governing policy" in section
    assert "never set `acknowledge_governance_change` yourself" in section.lower()


def test_skill_documents_case_three_same_policy_and_different_policies() -> None:
    text = _text()
    section = text[text.index("### Merging two capabilities") :]

    assert "not supported yet" not in section
    assert "error: governed capabilities are not supported yet" not in text
    assert "same policy" in section
    assert "no acknowledgment" in section.lower()
    assert "different policies" in section
    assert "release-capability-governance" in section
    assert "no completion path" in section
    assert "fork carries the whole governed set" in section


def test_skill_no_longer_claims_merging_is_unavailable_but_keeps_the_unsupported_cases() -> None:
    text = _text()

    assert "Merging, releasing governance and unmerging are not available yet" not in text
    assert "Merging obligations, merging a governed Capability" not in text
    assert "Unmerging is not available yet" not in text


def test_skill_walks_the_obligation_merge_flow_in_order() -> None:
    text = _text()
    section = text[text.index("### Merging two obligations") :]

    assert "merge-obligations" in section
    positions = [
        section.index(marker)
        for marker in (
            "**Candidate.**",
            "**Preview.**",
            "**Confirm.**",
            "**Passkey.**",
            "**Result.**",
        )
    ]
    assert positions == sorted(positions)


def test_skill_explains_what_an_obligation_merge_does_and_cannot_undo_by_a_tombstone() -> None:
    section = _text()[_text().index("### Merging two obligations") :]

    assert "same role" in section.lower()
    assert "deleted" in section
    assert "`SATISFIED_BY`" in section
    assert "`REQUIRES`" in section
    assert "audit" in section.lower()
    assert "MergedObligation" in section
    assert "never open, sign or complete it on the officer's behalf" in section.lower()


def test_skill_names_the_obligation_merge_errors() -> None:
    text = _text()

    assert "error: obligations under different roles cannot be merged" in text
    assert "error: an obligation cannot be merged into itself" in text
    assert "error: the survivor obligation does not exist" in text


def test_skill_walks_the_release_governance_flow_in_order() -> None:
    text = _text()
    section = text[text.index("### Releasing a capability from a policy") :]

    assert "release-capability-governance" in section
    positions = [
        section.index(marker)
        for marker in (
            "**Check.**",
            "**Preview.**",
            "**Confirm.**",
            "**Passkey.**",
            "**Result.**",
        )
    ]
    assert positions == sorted(positions)


def test_skill_explains_the_release_effect_and_the_draft_only_rule() -> None:
    section = _text()[_text().index("### Releasing a capability from a policy") :]

    assert "draft" in section
    assert "`GOVERNED_BY`" in section
    assert "capability.release_governance" in section
    assert "ungoverned" in section
    assert "never open, sign or complete it on the officer's behalf" in section.lower()
    assert "revert-policy-to-draft" in section


def test_skill_points_approved_policies_at_the_policy_lifecycle_and_states_the_fork_limit() -> None:
    section = _text()[_text().index("### Releasing a capability from a policy") :]

    assert "approved" in section
    assert "deprecated" in section
    assert "ps-policy-lifecycle" in section
    assert "fork carries the whole governed set" in section
    assert "no completion path" in section
    assert "do not invent a workaround" in section.lower()


def test_skill_names_the_release_governance_errors() -> None:
    text = _text()

    assert "release-capability-governance" in text
    assert "error: capability" in text
    assert "is not governed by any policy" in text
    assert "works only on a draft policy" in text
    assert "Releasing governance and unmerging" not in text


def test_skill_walks_the_capability_unmerge_flow_in_order() -> None:
    text = _text()
    section = text[text.index("### Unmerging a merge") :]

    assert "`unmerge`" in section
    positions = [
        section.index(marker)
        for marker in (
            "**Locate.**",
            "**Preview.**",
            "**Confirm.**",
            "**Passkey.**",
            "**Result.**",
        )
    ]
    assert positions == sorted(positions)


def test_skill_explains_what_a_capability_unmerge_restores_and_leaves_alone() -> None:
    section = _text()[_text().index("### Unmerging a merge") :]

    assert "`active`" in section
    assert "`MERGED_INTO`" in section
    assert "exactly the edges" in section
    assert "survivor_added_edges" in section
    assert "stay" in section
    assert "capability.unmerge" in section
    assert "audit" in section.lower()
    assert "never open, sign or complete it on the officer's behalf" in section.lower()


def test_skill_explains_unmerge_conflicts_and_that_nothing_is_forced() -> None:
    section = _text()[_text().index("### Unmerging a merge") :]

    assert "conflict" in section
    assert "no longer active" in section
    assert "governance" in section
    assert "no approval" in section.lower()
    assert "do not invent a workaround" in section.lower()


def test_skill_no_longer_says_unmerging_an_obligation_merge_is_unavailable() -> None:
    text = _text()

    assert "Unmerging an obligation merge is not available yet" not in text
    assert "unmerging an obligation merge is not available yet" not in text
    assert "Unmerging is not available yet" not in text


def test_skill_explains_what_an_obligation_unmerge_recreates_and_leaves_alone() -> None:
    section = _text()[_text().index("### Unmerging a merge") :]

    assert "obligation" in section.lower()
    assert "original id" in section
    assert "`MergedObligation`" in section
    assert "`HAS`" in section
    assert "`SATISFIED_BY`" in section
    assert "`REQUIRES`" in section
    assert "survivor_edges_possibly_from_merge" in section
    assert "may originate from the merge" in section
    assert "obligation.unmerge" in section


def test_skill_names_the_obligation_unmerge_conflicts() -> None:
    section = _text()[_text().index("### Unmerging a merge") :]

    assert "already exists again" in section
    assert "no `MergedObligation` marker" in section or "no MergedObligation marker" in section
    assert "survivor obligation" in section
    assert "tombstone" in section


# --- Slice 18: skill finalisation (case table, error table, E1 limitation, reconciliation) ----


def _section(heading: str) -> str:
    text = _text()
    start = text.index(heading)
    following = text.find(" ## ", start + len(heading))
    return text[start : following if following != -1 else len(text)]


def test_skill_has_a_merge_case_reference_covering_every_case() -> None:
    section = _section("## Merge case reference")

    for row in (
        "Case 1",
        "Case 2",
        "Case 3, same policy",
        "Case 3, different policies",
    ):
        assert row in section, row
    assert "acknowledge_governance_change" in section
    assert "no approval" in section.lower()
    assert "`release-capability-governance`" in section
    assert "merge-obligations" in section
    assert "same Role" in section


def test_skill_has_one_consolidated_error_reference_for_every_tool() -> None:
    section = _section("## Error reference")

    for tool in (
        "find-capability-merge-candidates",
        "find-duplicate-obligations",
        "merge-capabilities",
        "merge-obligations",
        "release-capability-governance",
        "unmerge",
        "check-cleanup-approval",
    ):
        assert f"`{tool}`" in section, tool
    for fragment in (
        "error: an unexpected error occurred",
        "error: no pending approval with id",
        "error: the approval status could not be settled right now; try again shortly",
        "error: the audit trail could not be read right now; try again shortly",
        "the graph changed since the preview",
        "could not be audited; nothing was changed",
        "check its status with check-cleanup-approval before retrying",
    ):
        assert fragment in section, fragment


def test_skill_states_the_approved_policy_limitation_and_the_follow_on() -> None:
    section = _section("## Known limitation")

    assert "two approved policies" in section
    assert "no completion path" in section
    assert "fork carries the whole governed set" in section
    assert "follow-on" in section
    assert "do not invent a workaround" in section.lower()


def test_skill_explains_reconciliation_of_an_interrupted_approval() -> None:
    section = _section("## Reconciliation of an interrupted approval")

    assert "five minutes" in section
    assert "reconciled: applied" in section
    assert "interrupted_no_effect" in section
    assert "`applied` audit row followed by a `failed` row" in section
    assert "never poll in a loop" in section.lower()
    assert "check-cleanup-approval" in section


def test_skill_is_complete_no_scope_so_far_language_and_all_seven_tools_have_a_flow() -> None:
    text = _text()

    assert "Scope covered so far" not in text
    assert "grows one step per released tool" not in text
    assert "not available yet" not in text
    for heading in (
        "### Finding capability merge candidates",
        "### Finding duplicate obligations",
        "### Merging two capabilities",
        "### Merging two obligations (same role)",
        "### Releasing a capability from a policy",
        "### Unmerging a merge",
        "## Merge case reference",
        "## Error reference",
        "## Known limitation",
        "## Reconciliation of an interrupted approval",
        "## Guardrails",
    ):
        assert heading in text, heading
    assert text.index("## Merge case reference") < text.index("## Guardrails")
