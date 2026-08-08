#!/usr/bin/env python3
"""Score a candidate query mechanism against a golden set.

DIRECTION OF TRAVEL IS ONE-WAY: this script runs the mechanism and diffs its
output against a pre-computed golden answer. It NEVER writes or updates a
golden answer. Golden answers are produced only by compute_answer.py through
an independent path. See golden/README.md -- "direction of travel."

This is a scaffold: it loads a questions.jsonl, dispatches each question to a
caller-supplied mechanism function, and reports per-question pass/fail plus a
per-set summary. Grading for exact-match and set-match is mechanical; rubric
and schema-gap kinds are reported as NEEDS-RUBRIC (human or rubric-scorer
grading) rather than auto-passed, so a rubric question can never silently
score as correct by string accident.

Usage:
    python3 score.py --set dev --mechanism <module.path:callable>

The mechanism callable takes a question string and returns a string answer.
It is imported, never exec'd, so the scored path is explicit and reviewable.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent.parent


def load_questions(set_name: str) -> list[dict]:
    path = GOLDEN_DIR / set_name / "questions.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no questions file: {path}")
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_golden(set_name: str, answer_ref: str) -> str:
    path = GOLDEN_DIR / set_name / answer_ref
    if not path.exists():
        raise FileNotFoundError(f"missing golden answer: {path}")
    return path.read_text()


def import_mechanism(spec: str):
    module_path, _, attr = spec.partition(":")
    if not attr:
        raise ValueError("--mechanism must be module.path:callable")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def grade_exact_or_set(mechanism_answer: str, golden: str) -> str:
    """Mechanical grading placeholder. Real exact/set diffing (id-set
    comparison, not string equality, per golden-answers.md's S3/M1 note) is
    implemented when the first set is actually scored -- deliberately not
    stubbed into a fake pass here."""
    raise NotImplementedError(
        "exact/set grading is not implemented in the scaffold; "
        "implement id-set diffing before scoring, do not string-compare"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, choices=["dev", "val", "test"])
    ap.add_argument("--mechanism", required=True,
                    help="module.path:callable taking a question str, returning an answer str")
    args = ap.parse_args()

    if args.set == "test":
        print("WARNING: scoring the TEST set. This should happen once, at a "
              "gate, against a frozen corpus (see test/FREEZE.md).", file=sys.stderr)

    questions = load_questions(args.set)
    mechanism = import_mechanism(args.mechanism)

    tuned = [q for q in questions if q.get("tuned_against")]
    if args.set == "test" and tuned:
        print(f"ERROR: test set contains {len(tuned)} tuned_against question(s); "
              "a tuned test set cannot support a generalization claim.", file=sys.stderr)
        return 2

    print(f"set={args.set}  questions={len(questions)}  "
          f"tuned_against={len(tuned)}")
    print("-" * 70)
    for q in questions:
        golden = load_golden(args.set, q["answer_ref"])
        answer = mechanism(q["question"])
        kind = q["kind"]
        if kind in ("exact-match", "set-match"):
            try:
                verdict = grade_exact_or_set(answer, golden)
            except NotImplementedError as exc:
                verdict = f"UNGRADED ({exc})"
        else:
            verdict = "NEEDS-RUBRIC (not auto-graded)"
        flag = " [TUNED]" if q.get("tuned_against") else ""
        print(f"{q['id']:<5} {kind:<11} {verdict}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
