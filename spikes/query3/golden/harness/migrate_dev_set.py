#!/usr/bin/env python3
"""One-shot, re-runnable migration: build the dev golden corpus.

Sources:
  - ../query1/golden-answers.md        -- answer bodies for the 39 golden questions
  - ../query1/example-questions.md     -- tier/anchor/kind metadata (encoded below, verbatim)
  - experiment_generalization_stress_test.py -- N1..N20 stress questions + expected routing
    (extracted via ast.literal_eval so this script does NOT need falkordb installed)

Outputs (idempotent -- safe to re-run):
  - dev/questions.jsonl      one record per question, per questions.schema.json
  - dev/answers/<ID>.md      golden answer / rubric / expected-routing note

Every record is emitted with tuned_against=true. That is deliberate and
load-bearing: all 59 questions were visible while matchers/templates were
being written, so none of them can anchor a generalization claim. See
golden/README.md.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPIKES_DIR = HERE.parents[2]  # .../spikes
GOLDEN_MD = SPIKES_DIR / "query1" / "golden-answers.md"
STRESS_PY = HERE.parents[1] / "experiment_generalization_stress_test.py"
DEV_DIR = HERE.parent / "dev"
ANSWERS_DIR = DEV_DIR / "answers"

SOURCE_GOLDEN = "query1/golden-answers.md"
SOURCE_STRESS = "query3/experiment_generalization_stress_test.py"

# ---------------------------------------------------------------------------
# The 39 golden questions. Question text verbatim from example-questions.md
# (post-correction phrasing, matching golden-answers.md). kind per the
# catalog's Grading column; H10/H15 are schema-gap refusals, not rubrics.
# ---------------------------------------------------------------------------
GOLDEN_QUESTIONS = [
    # id, tier, anchor, kind, question, notes
    ("S1", "simple", "named", "set-match",
     "What roles does GDPR define?", ""),
    ("S2", "simple", "named", "exact-match",
     "What's the text of CRA Article 13.1?",
     "Corrected from 'Article 11' -- out of CRA's extraction scope."),
    ("S3", "simple", "named", "set-match",
     "What obligations does the Manufacturer role carry under CRA?",
     "48 obligations; harness must diff id sets, not eyeball."),
    ("S4", "simple", "named", "set-match",
     "What capabilities does 'Maintain Security Logging' require?",
     "Corrected from 'Maintain Security Monitoring' -- no such obligation exists."),
    ("S5", "simple", "named", "exact-match",
     "When does CRA become effective, and what's its current status?", ""),
    ("S6", "simple", "named", "exact-match",
     "Which requirement does the 'Maintain Security Logging' obligation satisfy?",
     "Corrected from 'Maintain Structured Access Logging' -- same real obligation as S4, exercises the inbound edge."),
    ("S7", "simple", "named", "exact-match",
     "What policy governs the 'Security Logging' capability?", ""),
    ("S8", "simple", "named", "set-match",
     "List the standards under the Data Protection Policy.", ""),
    ("S9", "simple", "named", "set-match",
     "Which Controls exist under the Incident & Vulnerability Response Policy, and what are their statuses?", ""),
    ("S10", "simple", "named", "exact-match",
     "What's the implementation status of the Encryption-at-Rest control?", ""),
    ("M1", "medium", "open", "set-match",
     "Which capabilities are required by more than one obligation?",
     "52 capabilities; harness must diff id sets, not eyeball."),
    ("M2", "medium", "named", "exact-match",
     "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires.",
     "Corrected from 'Art. 11', same extraction-scope fix as S2."),
    ("M3", "medium", "named", "rubric",
     "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?",
     "Correct scope is CRA + HELVEX only; rubric requires explicitly stating NIS2/GDPR absence."),
    ("M4", "medium", "named", "exact-match",
     "How many obligations does GDPR place on Data Processors vs. Data Controllers?", ""),
    ("M5", "medium", "named", "rubric",
     "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?",
     "Role is deliberately non-canonical; no structural join exists. Correct answer presents both real role sets, no verdict."),
    ("M6", "medium", "open", "set-match",
     "Which obligations are backed by the weakest extraction confidence, and should be reviewed?",
     "Threshold choice (0.80) is a rubric call; 24 obligations at <=0.80."),
    ("M7", "medium", "named", "set-match",
     "Show every path from a GDPR requirement down to a Control that verifies it.",
     "57 chains (31 current-evidence / 26 stale). Original golden count was WRONG until an independent join caught the FalkorDB projection bug."),
    ("M8", "medium", "named", "set-match",
     "Which capabilities does our internal Helvex SOP regulation share with CRA?", ""),
    ("M9", "medium", "open", "exact-match",
     "How many Controls are currently overdue for review?",
     "Anchored to the fixture's 2026-08-01 reference date, not wall-clock today."),
    ("M10", "medium", "open", "exact-match",
     "What percentage of our Policies are still draft or deprecated rather than approved?", ""),
    ("M11", "medium", "open", "set-match",
     "Which Capabilities have a governing Policy but zero implemented Controls underneath?", ""),
    ("M12", "medium", "open", "set-match",
     "Which Controls are overdue for review right now (not just 'due soon')?",
     "Same underlying set as M9, different response shape; must derive from one shared filter."),
    ("M13", "medium", "named", "set-match",
     "Which Standards under the Data Protection & Security Policy are still in draft?",
     "Golden answer is the EMPTY SET -- tests confident 'none' vs. hallucination."),
    ("M14", "medium", "named", "rubric",
     "Which of our draft Policies are blocking GDPR readiness?", ""),
    ("H1", "hard", "named", "rubric",
     "Are we compliant with GDPR Article 32?",
     "Six sub-clauses: 2 clean, 1 partial, 1 stale, 2 entirely ungoverned. Original golden missed 32.1/32.1d until the same FalkorDB bug was re-hit."),
    ("H2", "hard", "named", "set-match",
     "Which capabilities required by CRA have no governing Policy yet?",
     "55 of 68 ungoverned."),
    ("H3", "hard", "open", "rubric",
     "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?", ""),
    ("H4", "hard", "named", "exact-match",
     "Show me the audit evidence that our log retention control passed last quarter.",
     "Correct answer is the evidence_ref pointer plus an explicit 'evidence store is out of scope' caveat."),
    ("H5", "hard", "named", "rubric",
     "NIS2 was updated — which of our Policies are now potentially out of date?", ""),
    ("H6", "hard", "named", "rubric",
     "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?",
     "Premise correction: SBOM capability already exists (CRA-only); correct answer is zero current redundant coverage."),
    ("H7", "hard", "open", "set-match",
     "Which of our automated controls are due for review in the next 30 days?",
     "Anchored to the fixture's 2026-08-01 reference date; exactly 2 controls."),
    ("H8", "hard", "open", "rubric",
     "I'm building a new microservice that stores customer PII in a database — what compliance capabilities should I be thinking about?", ""),
    ("H9", "hard", "open", "rubric",
     "Our security scanner flagged missing rate-limiting on an endpoint that processes health data — does that block a GDPR-relevant control?",
     "Correct answer names the absence: no rate-limiting Capability is modeled."),
    ("H10", "hard", "open", "schema-gap",
     "Is my service, `checkout-api`, currently compliant?",
     "Schema gap: no Service/System node exists. Correct answer is a named refusal."),
    ("H11", "hard", "open", "rubric",
     "If an attacker exploited a missing MFA control today, which regulatory obligations across CRA/NIS2/GDPR would we be out of compliance with?",
     "7 real obligations across all three regulations; hypothetical against today's approved-Policy/implemented-Control evidence."),
    ("H12", "hard", "open", "rubric",
     "Across our whole Control set, where are we most exposed — what would an auditor flag first?", ""),
    ("H13", "hard", "open", "rubric",
     "Give me a one-paragraph summary of our overall compliance posture I can bring to the board.", ""),
    ("H14", "hard", "open", "rubric",
     "What should my team prioritize this quarter to move the needle on compliance?", ""),
    ("H15", "hard", "open", "schema-gap",
     "How long, on average, does it take a Standard to go from draft to implemented in our organization?",
     "Schema gap: no status-transition history exists. Correct answer is a named refusal."),
]

# Answers that were authored alongside the mechanism and must be recomputed
# via harness/compute_answer.py before anchoring any holdout claim.
INDEPENDENT_RECOMPUTE_NEEDED = {"M7", "H1"}

REQUIRED_FIELDS = {
    "id", "set", "question", "tier", "kind", "author",
    "written_before_matcher", "tuned_against", "answer_ref",
}


def extract_golden_sections(text: str) -> dict[str, str]:
    """Split golden-answers.md into {question_id: body} by '### <ID> —' headers."""
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^### ([SMH]\d+)\b[^\n]*\n", text, re.M))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Trim a trailing tier divider if present
        body = text[start:end].rstrip()
        body = re.sub(r"\n---\s*$", "", body)
        sections[m.group(1)] = body
    return sections


def extract_stress_questions(path: Path) -> list[tuple[str, str, str]]:
    """Pull the QUESTIONS literal out of the stress test without importing it
    (avoids the falkordb dependency)."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "QUESTIONS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise RuntimeError(f"no QUESTIONS literal found in {path}")


def write_answer(qid: str, header: str, body: str) -> str:
    ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    rel = f"answers/{qid}.md"
    (ANSWERS_DIR / f"{qid}.md").write_text(header.rstrip() + "\n\n" + body.strip() + "\n")
    return rel


def validate(record: dict) -> None:
    missing = REQUIRED_FIELDS - record.keys()
    if missing:
        raise AssertionError(f"record {record.get('id')} missing required fields: {missing}")


def main() -> int:
    golden_text = GOLDEN_MD.read_text()
    sections = extract_golden_sections(golden_text)
    stress = extract_stress_questions(STRESS_PY)

    records: list[dict] = []

    # --- the 39 golden questions -------------------------------------------
    missing_sections = []
    for qid, tier, anchor, kind, question, notes in GOLDEN_QUESTIONS:
        body = sections.get(qid)
        if body is None:
            missing_sections.append(qid)
            body = "(answer body not found in golden-answers.md -- section header mismatch)"
        provenance = (
            f"# {qid} — golden answer (dev set)\n\n"
            f"- source: {SOURCE_GOLDEN}\n"
            f"- tuned_against: true (visible while matchers were written)\n"
            f"- independent_recompute_needed: "
            f"{'true — authored alongside query_mechanism_v1.py; recompute via harness/compute_answer.py' if qid in INDEPENDENT_RECOMPUTE_NEEDED else 'false'}"
        )
        answer_ref = write_answer(qid, provenance, body)
        rec = {
            "id": qid,
            "set": "dev",
            "question": question,
            "tier": tier,
            "kind": kind,
            "anchor": anchor,
            "author": "tma",
            "written_before_matcher": False,
            "tuned_against": True,
            "independent_recompute_needed": qid in INDEPENDENT_RECOMPUTE_NEEDED,
            "source_doc": SOURCE_GOLDEN,
            "answer_ref": answer_ref,
            "notes": notes,
        }
        validate(rec)
        records.append(rec)

    # --- the 20 stress questions --------------------------------------------
    for qid, question, expect in stress:
        body = (
            "No golden answer exists for this question. It is a routing probe, "
            "not a graded question.\n\n"
            f"**Pre-registered expectation (written before the run):** {expect}\n\n"
            "Score by comparing the mechanism's routing stage against that "
            "expectation, not by grading an answer body."
        )
        provenance = (
            f"# {qid} — expected routing (dev set, stress probe)\n\n"
            f"- source: {SOURCE_STRESS}\n"
            f"- tuned_against: true (written AFTER the matchers, deliberately probing them)\n"
            f"- independent_recompute_needed: false"
        )
        answer_ref = write_answer(qid, provenance, body)
        rec = {
            "id": qid,
            "set": "dev",
            "question": question,
            "tier": "stress",
            "kind": "rubric",
            "author": "tma",
            "written_before_matcher": False,
            "tuned_against": True,
            "independent_recompute_needed": False,
            "source_doc": SOURCE_STRESS,
            "answer_ref": answer_ref,
            "notes": f"Pre-registered routing expectation: {expect}",
        }
        validate(rec)
        records.append(rec)

    DEV_DIR.mkdir(parents=True, exist_ok=True)
    out = DEV_DIR / "questions.jsonl"
    with out.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"wrote {len(records)} records to {out}")
    print(f"  golden (S/M/H): {len(GOLDEN_QUESTIONS)}   stress (N): {len(stress)}")
    print(f"  all tuned_against=true; independent_recompute_needed: "
          f"{sorted(INDEPENDENT_RECOMPUTE_NEEDED)}")
    if missing_sections:
        print(f"  WARNING: {len(missing_sections)} answer bodies not found in "
              f"golden-answers.md: {missing_sections}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
