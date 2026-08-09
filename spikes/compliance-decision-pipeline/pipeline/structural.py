# © 2026 Cartman ApS. All rights reserved.
"""Stage 2 -- structural risk classification (README.md, "Stage 2").

Pure question-text classifier: no graph access, no dependency on Stage 1.
Two independent signals, either or both may fire on the same question:

- count_shaped: requires a tool-computed number at the fitness gate, never
  a hand-tally (targets the Miscount failure kind: AU-M2, SEC-M3, EM-H2).
- multi_part: requires decomposition into sub-claims, each checked before
  composing (targets the Completeness failure kind: SA-H2, PM-H1, PM-H2,
  RM-H2). Composition itself is Stage 3, deferred in v0 -- this stage only
  flags the *need* for it.

Keyword/regex heuristics, not NLP -- deliberately thin coverage. See
PROGRESS.md: no Setup-step-4 target cases are named for Stage 2 alone
(its targets are caught downstream, at Stage 4/composition), so this
module is spot-checked against the Miscount/Completeness case IDs rather
than validated against a strict must-flag/must-not-flag pair the way
Stage 1 and Stage 4's sub-checks are.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .types import Stage2Result

_COUNT_PATTERNS: List[str] = [
    r"\bhow many\b",
    r"\bhow much\b",
    r"\bnumber of\b",
    r"\bcount of\b",
    r"\btotal (?:number|count) of\b",
]

_MULTI_PART_PATTERNS: List[str] = [
    r"\ball applicable\b",
    r"\bexhaustive(?:ly)?\b",
    r"\bevery\b",
    r"\beach\b",
    r"\bas well as\b",
    r"\bboth\b",
    r"\bwhat.*and (?:what|which)\b",
    r"\bwhich.*and (?:what|which)\b",
]


def _find_signals(text: str, patterns: List[str]) -> Tuple[bool, List[str]]:
    hits = []
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            hits.append(m.group(0))
    return (len(hits) > 0, hits)


def run_stage2(question_id: str, question_text: str) -> Stage2Result:
    count_shaped, count_signals = _find_signals(question_text, _COUNT_PATTERNS)
    multi_part, multi_part_signals = _find_signals(question_text, _MULTI_PART_PATTERNS)

    # A second, cheap multi-part signal independent of the phrase list:
    # more than one '?' means the question is literally asking more than
    # one thing (e.g. SEC-M4: "...before the end of August 2026, and
    # which are already overdue?" only has one '?' despite being two
    # asks -- so this signal is a supplement, not a replacement, for the
    # phrase patterns above).
    if question_text.count("?") > 1:
        multi_part = True
        multi_part_signals.append("multiple '?' in question text")

    return Stage2Result(
        question_id=question_id,
        question_text=question_text,
        count_shaped=count_shaped,
        multi_part=multi_part,
        count_signals=count_signals,
        multi_part_signals=multi_part_signals,
    )
