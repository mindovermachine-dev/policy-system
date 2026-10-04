"""Structural/content lint for `ps-author-policy/SKILL.md` (issue #137, Slice 6, PLAN.md §1(a)).

Reads the real, finished `SKILL.md` off disk (no fakes needed -- this is a
pure content assertion, the "(a) Structural/content lint" mechanism PLAN.md
§1 describes) and asserts:

- Frontmatter has exactly `name: ps-author-policy` and a non-empty
  `description`, no other keys.
- The six required section headings are present, in order: `## Purpose`,
  `## On Load`, `## Core Principles`, `## Process`, `## Guardrails`,
  `## Output`.
- On Load text names the single connector `ps-mcp` verbatim, and the skill
  text names none of the old connector names.
- Guardrails text contains all four forbidden tool names verbatim:
  `propose-policy`, `approve-policy`, `reject-policy`,
  `revert-policy-to-draft` (AC-BI-010's "never calling it itself").
- Every one of the 9 tool names this skill actually calls (PLAN.md §0.3's
  table minus the four forbidden ones: `cypher`, `create-policy-draft`,
  `update-policy-draft`, `get-policy`, `add-standard-to-draft`,
  `update-standard-draft`, `add-control-to-draft`, `update-control-draft`,
  `domain_concepts`) appears at least once in the document body.
- Every named-exception error-message substring from PLAN.md §6.6's
  assembled Guardrails table appears somewhere in the Guardrails/Process
  text -- catching silent drift if a future `errors.py`/`mcp_server.py`
  message changes without this skill's table being updated. Each substring
  asserted here was itself re-verified against the real source
  (`ps_service.policy_lifecycle.errors`, `mcp_server.py`'s
  `_parse_patch_fields`/`add-control-to-draft`'s own `control_type` check,
  the `cypher` tool's own docstring) while writing this file, not copied
  from PLAN.md's paraphrase uncritically -- see IMPL_SLICE_6.md for the
  citations.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

_SKILL_PATH = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-author-policy"
    / "SKILL.md"
)

_REQUIRED_HEADINGS_IN_ORDER: tuple[str, ...] = (
    "## Purpose",
    "## On Load",
    "## Core Principles",
    "## Process",
    "## Guardrails",
    "## Output",
)

_CONNECTOR_NAMES: tuple[str, ...] = ("ps-mcp",)

# Built by concatenation so this file itself never spells the old names.
_OLD_GRAPH = "policy-system" + "-graph"

_FORBIDDEN_TOOL_NAMES: tuple[str, ...] = (
    "propose-policy",
    "approve-policy",
    "reject-policy",
    "revert-policy-to-draft",
)

# PLAN.md §0.3's table, minus the four forbidden tools above.
_CALLED_TOOL_NAMES: tuple[str, ...] = (
    "cypher",
    "create-policy-draft",
    "update-policy-draft",
    "get-policy",
    "add-standard-to-draft",
    "update-standard-draft",
    "add-control-to-draft",
    "update-control-draft",
    "domain_concepts",
)

# PLAN.md §6.6, each substring re-verified directly against
# `ps_service.policy_lifecycle.errors`/`mcp_server.py` while writing this
# file (IMPL_SLICE_6.md has the file:line citations).
_ERROR_MESSAGE_SUBSTRINGS: tuple[str, ...] = (
    "this action requires a real authenticated caller (the local-test bypass counts as one)",
    "the policy graph database is not reachable",
    "no Policy exists with id",
    "no Standard exists with id",
    "no Control exists with id",
    "you do not have access to this Policy",
    "already exists (id",
    "is not a patchable field",
    "must be a string or null",
    "must be one of",
    "control_type must be 'automated' or 'manual'",
    "cannot <action> a Policy in status",
    "cannot be superseded: current status is",
    "an unexpected error occurred",
)


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split a `---`-delimited YAML frontmatter block from the Markdown body below it."""
    assert text.startswith("---\n"), "SKILL.md must open with a `---` frontmatter fence"
    _, _, rest = text.partition("---\n")
    frontmatter_text, fence, body = rest.partition("\n---\n")
    assert fence, "SKILL.md's frontmatter fence was never closed"
    loaded = yaml.safe_load(frontmatter_text)
    assert isinstance(loaded, dict)
    frontmatter = cast("dict[str, object]", loaded)
    return frontmatter, body


def test_frontmatter_has_exactly_name_and_description() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    frontmatter, _body = _split_frontmatter(text)

    assert frontmatter.keys() == {"name", "description"}
    assert frontmatter["name"] == "ps-author-policy"
    assert isinstance(frontmatter["description"], str)
    assert frontmatter["description"].strip() != ""


def test_required_headings_present_in_order() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    heading_positions = [
        i for i, line in enumerate(lines) if line.strip() in _REQUIRED_HEADINGS_IN_ORDER
    ]
    found_headings = [lines[i].strip() for i in heading_positions]

    # Every required heading is present exactly once, in the required order
    # -- a copy-paste error dropping or reordering a section is caught here.
    assert found_headings == list(_REQUIRED_HEADINGS_IN_ORDER)


def _section_text(body: str, heading: str) -> str:
    """Return one `##`-level section's own text, up to (not including) the next `##` heading."""
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def test_on_load_names_the_connector_verbatim() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(text)
    on_load = _section_text(body, "## On Load")

    for connector_name in _CONNECTOR_NAMES:
        assert connector_name in on_load, f"On Load is missing connector name {connector_name!r}"


def test_skill_text_names_no_old_connector() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")

    assert _OLD_GRAPH not in text, "the skill still names an old connector"


def test_guardrails_names_all_four_forbidden_tools_verbatim() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(text)
    guardrails = _section_text(body, "## Guardrails")

    for tool_name in _FORBIDDEN_TOOL_NAMES:
        assert tool_name in guardrails, f"Guardrails is missing forbidden tool name {tool_name!r}"


def test_every_called_tool_name_appears_somewhere_in_the_body() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(text)

    for tool_name in _CALLED_TOOL_NAMES:
        assert tool_name in body, (
            f"{tool_name!r} (a tool this skill calls) never appears in the body"
        )


def test_every_named_error_message_substring_appears_in_guardrails_or_process() -> None:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(text)
    process = _section_text(body, "## Process")
    guardrails = _section_text(body, "## Guardrails")
    combined = process + "\n" + guardrails

    for substring in _ERROR_MESSAGE_SUBSTRINGS:
        assert substring in combined, (
            f"error-message substring {substring!r} never appears in Process/Guardrails "
            "-- either the Guardrails table drifted from the real source, or this test's "
            "own expectation needs updating against real source first"
        )


def test_never_describes_a_freshly_scaffolded_control_as_draft_implementation_status() -> None:
    """Process's own Control-loop instruction (PLAN.md §0.3/Slice 4): a freshly scaffolded
    Control's `implementation_status` server-defaults to `"planned"`, never `"draft"` --
    the governance `status` (always `"draft"` while in draft state) is a different property
    entirely. Guards against the two being conflated in prose (real drift Slice 4 found and
    fixed once already).
    """
    text = _SKILL_PATH.read_text(encoding="utf-8")
    _frontmatter, body = _split_frontmatter(text)
    process = _section_text(body, "## Process")
    process_one_line = " ".join(process.split())

    assert '"planned"' in process
    assert (
        "Never describe a freshly scaffolded Control's `implementation_status` as"
        in process_one_line
    )
