"""Content lint for `ps-graph-cleanup/SKILL.md` and its registrations.

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


_REFS = _SKILL.parent / "references"
_REFERENCES = {
    "release-governance.md": "when the user wants to release a capability",
    "unmerge.md": "when the user wants to reverse a capability, obligation or role merge",
    "split.md": "when the user wants to review automatic resolutions or split one",
    "merge-case-reference.md": "when explaining what a merge involves per governance case",
    "error-reference.md": "when a tool returns an error",
    "known-limitation.md": "when two capabilities are governed by two approved policies",
    "reconciliation.md": "when `check-cleanup-approval` reports a signed approval with no outcome",
}


def _text() -> str:
    return " ".join(_SKILL.read_text(encoding="utf-8").split())


def _ref(name: str) -> str:
    """A reference file's whitespace-normalised text (moved out of SKILL.md)."""
    return " ".join((_REFS / name).read_text(encoding="utf-8").split())


def _full() -> str:
    """SKILL.md plus every reference file, whitespace-normalised."""
    return " ".join([_text(), *(_ref(name) for name in _REFERENCES)])


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
    assert "tombstone" not in text
    assert "`MergedCapability` marker" in text
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


def test_skill_explains_what_an_obligation_merge_does_and_that_a_marker_remains() -> None:
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
    section = _ref("release-governance.md")

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
    section = _ref("release-governance.md")

    assert "draft" in section
    assert "`GOVERNED_BY`" in section
    assert "capability.release_governance" in section
    assert "ungoverned" in section
    assert "never open, sign or complete it on the officer's behalf" in section.lower()
    assert "revert-policy-to-draft" in section


def test_skill_points_approved_policies_at_the_policy_lifecycle_and_states_the_fork_limit() -> None:
    section = _ref("release-governance.md")

    assert "approved" in section
    assert "deprecated" in section
    assert "ps-policy-lifecycle" in section
    assert "fork carries the whole governed set" in section
    assert "no completion path" in section
    assert "do not invent a workaround" in section.lower()


def test_skill_names_the_release_governance_errors() -> None:
    text = _ref("release-governance.md")

    assert "release-capability-governance" in text
    assert "error: capability" in text
    assert "is not governed by any policy" in text
    assert "works only on a draft policy" in text
    assert "Releasing governance and unmerging" not in _full()


def test_skill_walks_the_capability_unmerge_flow_in_order() -> None:
    section = _ref("unmerge.md")

    assert "`unmerge-entities`" in section
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
    section = _ref("unmerge.md")

    assert "`active`" in section
    assert "`MergedCapability`" in section
    assert "tombstone" not in section
    assert "exactly the edges" in section
    assert "survivor_added_edges" in section
    assert "stay" in section
    assert "capability.unmerge" in section
    assert "audit" in section.lower()
    assert "never open, sign or complete it on the officer's behalf" in section.lower()


def test_skill_explains_unmerge_conflicts_and_that_nothing_is_forced() -> None:
    section = _ref("unmerge.md")

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
    section = _ref("unmerge.md")

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
    section = _ref("unmerge.md")

    assert "already exists again" in section
    assert "no `MergedObligation` marker" in section or "no MergedObligation marker" in section
    assert "survivor obligation" in section
    assert "merged away" in section


# --- skill finalisation (case table, error table, E1 limitation, reconciliation) ----


def _section(heading: str, reference: str) -> str:
    text = _ref(reference)
    start = text.index(heading)
    following = text.find(" ## ", start + len(heading))
    return text[start : following if following != -1 else len(text)]


def test_skill_has_a_merge_case_reference_covering_every_case() -> None:
    section = _section("## Merge case reference", "merge-case-reference.md")

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
    section = _section("## Error reference", "error-reference.md")

    for tool in (
        "find-capability-merge-candidates",
        "find-duplicate-obligations",
        "merge-capabilities",
        "merge-obligations",
        "release-capability-governance",
        "unmerge-entities",
        "list-capability-resolutions",
        "split-capability",
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
    section = _section("## Known limitation", "known-limitation.md")

    assert "two approved policies" in section
    assert "no completion path" in section
    assert "fork carries the whole governed set" in section
    assert "follow-on" in section
    assert "do not invent a workaround" in section.lower()


def test_skill_explains_reconciliation_of_an_interrupted_approval() -> None:
    section = _section("## Reconciliation of an interrupted approval", "reconciliation.md")

    assert "five minutes" in section
    assert "reconciled: applied" in section
    assert "interrupted_no_effect" in section
    assert "`applied` audit row followed by a `failed` row" in section
    assert "never poll in a loop" in section.lower()
    assert "check-cleanup-approval" in section


def test_skill_is_complete_no_scope_so_far_language_and_all_seven_tools_have_a_flow() -> None:
    text = _full()

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
    assert "## Guardrails" in _text()


def test_issue_200_on_demand_references_exist_and_are_named_with_their_condition() -> None:
    skill = _text()

    for name, condition in _REFERENCES.items():
        assert (_REFS / name).is_file(), f"{name} is missing"
        assert f"read `references/{name}`" in skill, f"SKILL.md never says when to read {name}"
        assert condition in skill.lower(), name
        assert "Guardrail" in _ref(name), f"{name} must say the Guardrails still apply"


# --- Role merge and Role unmerge ------------------------------------------------------


def test_skill_walks_the_role_merge_flow_in_order() -> None:
    text = _text()
    section = text[text.index("### Merging two roles") :]

    assert "merge-roles" in section
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


def test_skill_explains_what_a_role_merge_does_and_the_same_instrument_rule() -> None:
    section = _text()[_text().index("### Merging two roles") :]

    assert "same instrument" in section.lower()
    assert "`MergedRole`" in section
    assert "`HAS`" in section
    assert "`DEFINES`" in section
    assert "keep their ids" in section
    assert "role.merge" in section
    assert "duplicate_obligation_pairs" in section
    assert "find-duplicate-obligations" in section
    assert "review_id" in section
    assert "never open, sign or complete it on the officer's behalf" in section.lower()


def test_skill_sources_a_role_pair_from_the_officer_or_read_only_cypher() -> None:
    section = _text()[_text().index("### Merging two roles") :]

    assert "no Role discovery tool" in section
    assert "read-only `run-cypher`" in section
    assert "get-domain-concepts" in section


def test_skill_names_the_role_merge_errors() -> None:
    text = _text()

    assert "error: a role cannot be merged into itself" in text
    assert "error: roles defined by different instruments cannot be merged" in text
    assert "MergedRole marker" in text
    assert "error: the review is not a Role review" in text


def test_skill_scope_and_description_mention_roles() -> None:
    raw = _SKILL.read_text(encoding="utf-8")
    description = raw.split("---\n")[1]

    assert "merge-roles" in _text()
    assert "Role" in description
    assert "unmerge a Capability, Obligation or Role merge" in description


def test_unmerge_reference_covers_the_role_kind() -> None:
    section = _ref("unmerge.md")

    assert "`merge-roles`" in section
    assert "`MergedRole`" in section
    assert "role.merge" in section
    assert "role.unmerge" in section
    assert "obligations_to_restore" in section
    assert "survivor_obligations_added_since_merge" in section
    assert "defines_source_ref" in section
    assert "stay on the survivor" in section
    assert "audit event" in section
    assert "reverse that one first" in section


def test_unmerge_reference_names_the_role_conflicts_and_states() -> None:
    section = _ref("unmerge.md")

    assert "already exists again" in section
    assert "no longer exists" in section
    assert "later `obligation.merge`" in section
    assert "later `role.merge`" in section
    assert "Dependent merge" in section
    assert "error: role ... cannot be unmerged: a later ..." in section


def test_error_and_case_references_cover_merge_roles() -> None:
    errors = _section("## Error reference", "error-reference.md")
    cases = _section("## Merge case reference", "merge-case-reference.md")

    assert "`merge-roles`" in errors
    assert "different instruments" in errors
    assert "`merge-roles`" in cases
    assert "same instrument" in cases.lower()


def test_unmerge_reference_says_a_resolution_marker_cannot_be_unmerged() -> None:
    section = _ref("unmerge.md")

    assert "`ResolvedCapability`" in section
    assert "is not a merge and cannot be unmerged" in section
    assert "`split-capability`" in section
    assert "`references/split.md`" in section


# --- Reviewing and splitting automatic resolutions -----------------------------------


def _split_section() -> str:
    text = _text()
    start = text.index("### Reviewing and splitting automatic resolutions")
    following = text.find(" ### ", start + 1)
    return text[start : following if following != -1 else len(text)]


def test_split_section_orders_list_preview_ack_before_url() -> None:
    section = _split_section()

    steps = (
        "**List.**",
        "**Choose.**",
        "**Preview.**",
        "**Acknowledge.**",
        "**Passkey.**",
        "**Result.**",
    )
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)
    bodies = {
        step: section[start:end]
        for step, start, end in zip(steps, positions, [*positions[1:], len(section)], strict=True)
    }
    assert "`list-capability-resolutions`" in bodies["**List.**"]
    assert "never choose for the user" in bodies["**Choose.**"].lower()
    assert "`split-capability`" in bodies["**Preview.**"]
    assert "`x_id`" in bodies["**Preview.**"]
    acknowledge = bodies["**Acknowledge.**"]
    assert "`acknowledgment_required: true`" in acknowledge
    assert "explicit" in acknowledge.lower()
    assert "`acknowledge_governance_change`" in acknowledge
    assert "no approval" in acknowledge.lower()
    assert "`approval_url`" not in bodies["**List.**"] + bodies["**Preview.**"] + acknowledge
    assert "`approval_url`" in bodies["**Passkey.**"]
    assert "`check-cleanup-approval`" in bodies["**Result.**"]
    assert "`split: true`" in bodies["**Result.**"]


def test_split_section_says_what_the_list_shows_and_that_it_changes_nothing() -> None:
    section = _split_section()

    for field in ("`x_id`", "`a_id`", "`governing_policy`", "`similarity`", "`instruments`"):
        assert field in section, field
    assert "`obligation_count`" in section
    assert "read-only" in section
    assert "never acknowledge" in section.lower()


def test_split_reference_states_the_edge_rule_the_result_and_the_reversal() -> None:
    section = _ref("split.md")

    for bucket in ("`moves`", "`shared`", "`added`", "`stays`"):
        assert bucket in section, bucket
    assert "`capability.split`" in section
    assert "no governing policy" in section.lower()
    assert "`merge-capabilities`" in section
    assert "`split: true`" in section
    assert "`ResolvedCapability`" in section
    assert "`references/error-reference.md`" in section


def test_skill_scope_guardrails_and_description_cover_the_split() -> None:
    raw = _SKILL.read_text(encoding="utf-8")
    description = " ".join(raw.split("---\n")[1].split())
    text = _text()
    guardrails = text[text.index("## Guardrails") :]

    assert "list the Capabilities the pipeline resolved automatically" in description
    assert "split one back out" in description
    assert "`list-capability-resolutions`" in text[: text.index("## On Load")]
    assert "`split-capability`" in text[: text.index("## On Load")]
    assert "`split-capability`" in guardrails
    readme = (_REPO / "ps-skills" / "readme.md").read_text(encoding="utf-8")
    assert "splits a Capability the pipeline resolved automatically" in readme
