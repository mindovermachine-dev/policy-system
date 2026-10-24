"""Content lint for `ps-near-miss-review/SKILL.md`: the batch merge flow.

Reads the real `SKILL.md` off disk. The merge path is one flow for a user-chosen set of reviews:
the effect is stated first, the user names the reviews, each governance change is acknowledged
per review, the listed preview and the approval link are shown, and the per-merge outcomes come
from `check-near-miss-merges`.
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[3]
    / "ps-skills"
    / "ps-plugin"
    / "skills"
    / "ps-near-miss-review"
    / "SKILL.md"
)
_MERGE_HEADING = "### Merging chosen reviews under one approval"


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def _merge_section() -> str:
    lines = _skill().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == _MERGE_HEADING)
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"#{2,3} ", lines[i])),
        len(lines),
    )
    return _normalise("\n".join(lines[start:end]))


def _position(section: str, marker: str) -> int:
    assert marker in section, f"merge flow never mentions {marker!r}"
    return section.index(marker)


def test_the_merge_flow_uses_the_batch_tools_only() -> None:
    text = _skill()

    assert "merge-near-misses" in text
    assert "check-near-miss-merges" in text
    assert 'decision="merge"' not in text
    assert "acknowledge_governance_change" not in text
    assert "pending_approval_id" in _merge_section()


def test_the_effect_is_stated_before_the_user_is_asked_to_choose() -> None:
    section = _merge_section()

    assert _position(section, "State the effect first") < _position(section, "name the reviews")


def test_the_user_names_the_set_of_reviews_and_the_skill_never_picks_it() -> None:
    section = _merge_section()

    assert "the user names which reviews to merge" in section
    assert "never choose, add or drop a review" in section
    assert _position(section, "name the reviews") < _position(section, "`merge-near-misses`")


def test_each_governance_change_needs_its_own_explicit_acknowledgment() -> None:
    section = _merge_section()

    assert "acknowledged_review_ids" in section
    assert "one review at a time" in section
    assert "never acknowledge a review the officer has not accepted by name" in section
    assert "never acknowledge all" in section
    assert "A Role review takes no acknowledgment" in section


def test_the_listed_preview_and_the_approval_link_are_shown_before_signing() -> None:
    section = _merge_section()

    preview = _position(section, "one entry per review")
    link = _position(section, "`approval_url`")
    assert preview < link
    for field in ("survivor", "absorbed", "state_digest", "expires_at"):
        assert field in section
    assert "nothing has changed yet" in section


def test_the_per_merge_outcomes_come_from_the_status_tool() -> None:
    section = _merge_section()

    assert _position(section, "`approval_url`") < _position(section, "`check-near-miss-merges`")
    for state in ("merged", "failed", "unsettled"):
        assert f"`{state}`" in section
    assert "winner_id" in section
    assert "loser_id" in section


def test_skill_treats_an_unsettled_merge_as_not_yet_known() -> None:
    section = _merge_section()

    assert "not yet known" in section
    assert "check again later" in section
    assert "never report an `unsettled` merge as done or as failed" in section


def test_the_skill_does_not_describe_a_one_review_merge() -> None:
    text = _normalise(_skill())

    assert 'resolve-near-miss` with `review_id` and `decision="merge"' not in text
    assert "check-near-miss-" + "approval" not in text
