"""The documents describe the automatic-resolution tools where a reader looks for them.

The Container Architecture lists every MCP tool of the Graph Cleanup group in three places (the
MCP Interface tool list, the tool-group registration row and the gate table), and specifies each
tool once as a Graph Cleanup action, with the split's constraint, audit action and modules. The
user guide explains what each list entry shows and what a split does, and both copies of the domain
concepts describe the split in the Capability lifecycle. The Compliance Officer's cleanup use case
walks the review and the split as a beat, names its contracts and keeps the open point on where a
correction of a pipeline decision belongs.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_CONTAINER_ARCHITECTURE = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"
_DOMAIN_CONCEPTS = _REPO / "docs" / "artifacts" / "ps-domain-concepts.md"
_PACKAGED_DOMAIN_CONCEPTS = (
    _REPO / "ps-service" / "src" / "ps_service" / "mcp_interface" / "ps-domain-concepts.md"
)
_CLEANUP_USE_CASE = (
    _REPO
    / "docs"
    / "architecture"
    / "ps-client-use-cases"
    / "cuc-07-merge-duplicate-capabilities-obligations.md"
)
_SKILL_ERROR_REFERENCE = (
    _REPO
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-graph-cleanup"
    / "references"
    / "error-reference.md"
)


def _lines_naming(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


def test_the_container_architecture_lists_the_resolution_tools_with_their_group() -> None:
    text = _CONTAINER_ARCHITECTURE.read_text(encoding="utf-8")

    group_lines = [
        line
        for line in _lines_naming(text, "`list-capability-resolutions`")
        if "`find-duplicate-obligations`" in line
        and "`check-cleanup-approval`" in line
        and "`split-capability`" in line
    ]

    assert len(group_lines) == 3


def test_the_container_architecture_specifies_the_resolution_list_action() -> None:
    text = _CONTAINER_ARCHITECTURE.read_text(encoding="utf-8")

    [action_row] = [
        line for line in text.splitlines() if line.startswith("| ListCapabilityResolutions |")
    ]
    assert "`ResolvedCapability`" in action_row
    assert "read-only" in action_row
    assert "cosine similarity" in action_row
    assert "relying on X" in action_row
    assert _lines_naming(text, "`ps-service/src/ps_service/graph_cleanup/baseline_reader.py`")


def test_the_user_guide_explains_what_each_automatic_resolution_entry_shows() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    [paragraph] = [
        block
        for block in text.split("\n\n")
        if block.startswith("**Automatic resolutions (read-only).**")
    ]
    for fragment in (
        "`list-capability-resolutions`",
        "`ResolvedCapability`",
        "similarity",
        "instruments",
        "Obligations rely on it",
        "changes nothing",
    ):
        assert fragment in paragraph


def test_the_container_architecture_specifies_the_split_preview_and_approval() -> None:
    text = _CONTAINER_ARCHITECTURE.read_text(encoding="utf-8")

    [action_row] = [line for line in text.splitlines() if line.startswith("| SplitCapability |")]
    for fragment in (
        "`split-capability`",
        "`ResolvedCapability`",
        "moves",
        "stays",
        "governing Policy",
        "acknowledgment_required",
        "creates no approval",
        "state digest",
    ):
        assert fragment in action_row
    [baseline_row] = _lines_naming(
        text, "`ps-service/src/ps_service/graph_cleanup/baseline_reader.py`"
    )
    for edge_type in ("`REQUIRES`", "`COVERS`", "`MITIGATED_BY`"):
        assert edge_type in baseline_row
    [service_row] = _lines_naming(text, "`ps-service/src/ps_service/graph_cleanup/service.py`")
    assert "SplitCapability" in service_row


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_container_architecture_specifies_the_split_execution() -> None:
    text = _CONTAINER_ARCHITECTURE.read_text(encoding="utf-8")

    [constraint] = [
        line
        for line in text.splitlines()
        if line.startswith("| Split of an automatic resolution |")
    ]
    for fragment in (
        "`split-capability`",
        "some baseline holds",
        "no governing Policy",
        "`capability.split`",
        "`merge-capabilities`",
    ):
        assert fragment in constraint
    [actions] = _lines_naming(text, "- Registered audit actions:")
    assert "`capability.split`" in actions
    [audit_row] = _lines_naming(text, "`ps-service/src/ps_service/graph_cleanup/audit_actions.py`")
    assert "eight audit actions" in audit_row
    [reader_row] = _lines_naming(text, "`ps-service/src/ps_service/graph_cleanup/split_reader.py`")
    assert "SplitCapability" in reader_row
    [tool_name_row] = [line for line in text.splitlines() if line.startswith("| `tool_name` |")]
    assert "`split-capability`" in tool_name_row
    [action_row] = [line for line in text.splitlines() if line.startswith("| SplitCapability |")]
    assert "`capability.split`" in action_row


def test_both_domain_concept_copies_describe_the_split_in_the_capability_lifecycle() -> None:
    docs = _DOMAIN_CONCEPTS.read_text(encoding="utf-8")
    packaged = _PACKAGED_DOMAIN_CONCEPTS.read_text(encoding="utf-8")

    assert docs == packaged
    [lifecycle] = [
        _flat(block)
        for block in docs.split("\n\n")
        if block.startswith("**Lifecycle:**") and "`ResolvedCapability`" in block
    ]
    for fragment in (
        "`list-capability-resolutions`",
        "`split-capability`",
        "`capability.split`",
        "starts with no governing Policy",
        "reversed by a cleanup merge",
    ):
        assert fragment in lifecycle


def test_the_user_guide_explains_the_split_and_lists_its_audit_action() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    [paragraph] = [
        _flat(block)
        for block in text.split("\n\n")
        if block.startswith("**Splitting an automatic resolution.**")
    ]
    for fragment in (
        "`split-capability`",
        "acknowledge",
        "no governing policy",
        "`capability.split`",
        "`merge-capabilities`",
    ):
        assert fragment in paragraph
    [filters] = [_flat(block) for block in text.split("\n\n") if "Filter by the actions" in block]
    assert "`capability.split`" in filters


def _table_rows(text: str, first_cell: str) -> list[str]:
    return [
        _flat(line)
        for line in text.splitlines()
        if line.startswith("|") and line.split("|")[1].strip() == first_cell
    ]


def test_the_skill_error_reference_names_every_split_refusal() -> None:
    text = _SKILL_ERROR_REFERENCE.read_text(encoding="utf-8")

    [listing] = _table_rows(text, "`list-capability-resolutions`")
    assert "changed nothing" in listing
    split_rows = _table_rows(text, "`split-capability`")
    [preview] = [row for row in split_rows if "rejected at preview" in row]
    for fragment in (
        "already a Capability",
        "merged by a cleanup merge",
        "no automatic resolution",
        "no baseline holds it",
        "no approval exists",
    ):
        assert fragment in preview
    [acknowledgment] = [row for row in split_rows if "`acknowledgment_required`" in row]
    assert "`acknowledge_governance_change`" in acknowledgment
    assert "not an error" in acknowledgment
    signed = " ".join(_table_rows(text, "any signed approval"))
    for fragment in (
        "the approver no longer holds the required role",
        "the governance change was not acknowledged in this approval",
        "the policy graph database could not be reached; nothing was changed",
    ):
        assert fragment in signed
    assert "`split: true`" in _flat(text)


def test_the_container_architecture_specifies_the_audited_signer_refusal() -> None:
    text = _CONTAINER_ARCHITECTURE.read_text(encoding="utf-8")

    [actor_bound] = _table_rows(text, "Actor-bound")
    for fragment in ("verifies", "voided", "`failed`", "`signer_not_bound_actor`", "no row"):
        assert fragment in actor_bound
    [sign_verify] = _table_rows(text, "PostSignVerify")
    assert "`signer_not_bound_actor`" in sign_verify
    [registry_row] = _lines_naming(text, "`ps-service/src/ps_service/passkey_signing/executors.py`")
    assert "register_signing_refusal_recorder" in registry_row
    [recorder_row] = _lines_naming(
        text, "`ps-service/src/ps_service/graph_cleanup/signing_refusals.py`"
    )
    assert "`merge-near-misses`" in recorder_row


def test_the_user_guide_explains_an_approval_signed_by_someone_else() -> None:
    text = _USER_GUIDE.read_text(encoding="utf-8")

    [paragraph] = [
        _flat(block)
        for block in text.split("\n\n")
        if block.startswith("**An approval signed by someone else.**")
    ]
    for fragment in ("`failed`", "`signer_not_bound_actor`", "new approval", "records nothing"):
        assert fragment in paragraph


def _beat(text: str, label: str) -> str:
    """One numbered beat of a use case: from its bold label to the next item or blank line."""
    start = text.index(f"**{label}.**")
    ends = [match.start() for match in re.finditer(r"\n(\d+\. |\n)", text[start:])]
    return _flat(text[start : start + ends[0]] if ends else text[start:])


def test_the_cleanup_use_case_walks_the_review_and_split_of_an_automatic_resolution() -> None:
    text = _CLEANUP_USE_CASE.read_text(encoding="utf-8")
    flat = _flat(text)

    beat = _beat(text, "Split an automatic resolution")
    for fragment in (
        "`list-capability-resolutions`",
        "`split-capability`",
        "`acknowledgment_required`",
        "before any approval exists",
        "no governing Policy",
        "`capability.split`",
        "`merge-capabilities`",
    ):
        assert fragment in beat, fragment
    contracts = flat[flat.index("## Contracts touched") : flat.index("## Open points")]
    assert "`list-capability-resolutions`" in contracts
    assert "`split-capability`" in contracts
    open_points = flat[flat.index("## Open points") :]
    assert (
        "A correction of a pipeline decision could instead follow CUC-06's review/resolve shape"
        " or have its own use case; it is kept here because it is undone by a cleanup merge."
    ) in open_points
    assert "split" in flat[: flat.index("## Entry points")].lower()
