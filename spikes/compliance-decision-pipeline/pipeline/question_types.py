# © 2026 Cartman ApS. All rights reserved.
"""The question-type reliability table from README.md's "What we can
reliably answer, by question type" and "Output posture" sections,
transcribed verbatim (not re-derived here -- this module is not where that
classification happens, it's the data compose.py reads to generate (A)'s
text, per README's "Build implication": the confidence-statement text is
generated from this table, not hardcoded per-question).

Assigning a question its type (A-H) is currently a first-pass human
judgment call made while reading question text (README.md), not something
any pipeline stage computes -- there is no type-classifier module. Callers
of compose_output() pass the type in.
"""

from __future__ import annotations

from typing import Dict

# name + measured reliability, exactly as reported in README.md's table.
TYPE_RELIABILITY: Dict[str, Dict[str, str]] = {
    "A": {"name": "Single-fact lookup", "measured": "26/26 -- 100%"},
    "B": {"name": "Exact-set enumeration", "measured": "32/38 -- 84%"},
    "C": {"name": "Aggregate/count", "measured": "15/19 -- 79%"},
    "D": {"name": "Chain/multi-hop trace", "measured": "9/12 -- 75%"},
    "E": {"name": "Cross-entity/regulation comparison", "measured": "18/20 -- 90%"},
    "F": {"name": "Status/definitional judgment", "measured": "9/16 -- 56% overall (1/5, 20% on the CLI path)"},
    "G": {"name": "Open recommendation/critique", "measured": "10/20 -- 50%, both tool surfaces"},
    "H": {"name": "Refusal-expected (gap check)", "measured": "7/7 once the Known-Gaps Registry exists"},
}

_CONFIDENT_TEXT = "Given the data currently in the system, this is correct."

# F and G get a hedge instead of the confident text, unconditionally --
# README: "Type G specifically -> bias toward a hedged block ... as the
# *default*, not the fallback" (measured ~50% holds on both tool surfaces,
# no known fix to attempt first). F is the same shape today (no
# CLI-summarization fix has landed yet to justify the confident text on
# this path) even though README frames F as "looks fixable."
_HEDGE_TEXT: Dict[str, str] = {
    "F": (
        "Best-effort answer. Questions of this kind matched expert grading "
        "in ~56% of validation cases; on this system's current answering "
        "path specifically, ~20% -- this looks like a fixable summarization "
        "gap, not a fundamental limit. Verify against (C) before relying on "
        "this."
    ),
    "G": (
        "Best-effort answer. Questions of this kind matched expert grading "
        "in ~50% of validation cases, consistently across two "
        "independently-built answering paths -- this is a draft requiring "
        "human judgment, not a verified conclusion. Verify against (C)."
    ),
}


def confidence_statement_for_type(question_type: str) -> str:
    """The (A) text for a question whose type is known and whose fitness
    gate (or, for H, the Known-Gaps Registry check) has cleared. Does not
    handle the undefined-term-refusal or gate-failure cases -- those are
    generated in compose.py from Stage 1/4 results directly, not from this
    table, because their text names the specific term/reason, not the type.
    """
    if question_type not in TYPE_RELIABILITY:
        raise ValueError(f"unknown question type {question_type!r} -- must be one of {sorted(TYPE_RELIABILITY)}")
    return _HEDGE_TEXT.get(question_type, _CONFIDENT_TEXT)
