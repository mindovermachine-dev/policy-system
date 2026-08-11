#!/usr/bin/python3
# Hardcoded, not `env python3` -- same reason as ps.py (README.md): the
# repo .venv's python3 lacks the falkordb package, and pipeline/ps_client.py
# shells out to ps.py, which needs it. /usr/bin/python3 is the only
# interpreter with it installed.
"""Pipeline CLI entrypoint (README.md gap 1) -- the live path AD-7 needs:
a callable `pipeline query "<question>" --type <A-H> --answer <prose>
--claims <json>` command wiring Stage 1 -> Stage 2 -> Stage 3 (routing) ->
the claim-schema adapter (pipeline/adapter.py, dispatching structured
claims to Stage 4) -> pipeline.compose.compose_output's three-block
output.

`--type` is a required flag, not something this CLI infers:
`pipeline/question_types.py`'s own docstring is explicit that question-type
(A-H) assignment is "currently a first-pass human judgment call," not
something any pipeline stage computes -- callers supply it. Here, "caller"
is the harness asking the question, the same way it supplies `--claims`
per PROGRESS.md's D1 discipline (structured input from the harness, not
re-derived by the pipeline).

`--answer` is the answer's prose (composed into block B); `--claims` is
the separate, structured D1 payload used to run Stage 4 verification --
conflating the two would let prose stand in for evidence, which is the
exact gap D1 closed.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

from pipeline import ps_client
from pipeline.adapter import AdapterError, dispatch_claims
from pipeline.alias_table import run_stage1
from pipeline.compose import compose_output
from pipeline.question_types import TYPE_RELIABILITY
from pipeline.routing import route_question
from pipeline.structural import run_stage2
from pipeline.types import Claim, ClaimKind, ClaimSet

_CLAIM_LIST_FIELDS = {
    ClaimKind.OVERDUE_SET: "control_ids",
    ClaimKind.CITED_IDS: "ids",
    ClaimKind.REGULATION_SCOPE: "regulations",
}


def _question_id(question_text: str) -> str:
    # Stable across repeated runs of the same question text -- deliberate,
    # so this session's live-question log (README.md Deliverables) can
    # de-duplicate re-runs of the same question by id.
    return "live_" + hashlib.sha256(question_text.encode("utf-8")).hexdigest()[:8]


def _parse_claim_set(question_id: str, raw: dict) -> ClaimSet:
    claims = []
    for i, entry in enumerate(raw.get("claims", [])):
        if "kind" not in entry:
            raise ValueError(f"claim[{i}] missing required field 'kind'")
        try:
            kind = ClaimKind(entry["kind"])
        except ValueError:
            raise ValueError(
                f"claim[{i}] has unknown kind {entry['kind']!r} -- must be one of "
                f"{sorted(k.value for k in ClaimKind)}"
            )
        kwargs = {
            "kind": kind,
            "capability_id": entry.get("capability_id"),
            "entity_type": entry.get("entity_type"),
            "count": entry.get("count"),
            "category": entry.get("category"),
        }
        list_field = _CLAIM_LIST_FIELDS.get(kind)
        if list_field is not None:
            kwargs[list_field] = frozenset(entry.get(list_field, []))
        claims.append(Claim(**kwargs))
    return ClaimSet(question_id=question_id, claims=claims)


def _render_text(output) -> str:
    lines = [
        f"question_id: {output.question_id}",
        "",
        "(A) Confidence",
        output.confidence_statement,
        "",
        "(B) Answer",
        output.answer,
        "",
        "(C) Verification data",
        json.dumps(output.verification_data, indent=2, default=str),
    ]
    return "\n".join(lines)


def cmd_query(args):
    question_id = args.question_id or _question_id(args.question)

    if args.claims_file:
        raw_claims = json.loads(Path(args.claims_file).read_text())
    elif args.claims:
        raw_claims = json.loads(args.claims)
    else:
        raw_claims = {"claims": []}

    try:
        claim_set = _parse_claim_set(question_id, raw_claims)
    except ValueError as e:
        print(f"error: invalid --claims payload: {e}", file=sys.stderr)
        return 1

    stage1 = run_stage1(question_id, args.question)
    stage2 = run_stage2(question_id, args.question)
    routing = route_question(stage1, stage2, args.type)

    try:
        fitness_result = dispatch_claims(claim_set, stage1, args.reference_date)
    except AdapterError as e:
        print(f"error: claim dispatch failed: {e}", file=sys.stderr)
        return 1
    except ps_client.PsClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    output = compose_output(
        question_id=question_id,
        question_type=args.type,
        stage1=stage1,
        stage2=stage2,
        fitness=fitness_result,
        proposed_answer=args.answer,
        routing=routing,
    )

    if args.format == "json":
        print(
            json.dumps(
                {
                    "question_id": output.question_id,
                    "confidence_statement": output.confidence_statement,
                    "answer": output.answer,
                    "verification_data": output.verification_data,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(_render_text(output))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="PS Answer Verification Pipeline CLI (spikes/e2e-pipeline, AD-7's live loop).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_query = sub.add_parser(
        "query",
        help="Verify a live question's proposed answer against the graph",
        description=(
            "Runs Stage 1 (term coverage) -> Stage 2 (structural flags) -> "
            "Stage 3 (routing) -> the claim-schema adapter (Stage 4 "
            "dispatch) -> the three-block composer, for one question."
        ),
    )
    p_query.add_argument("question", help="The question, verbatim, in natural language")
    p_query.add_argument(
        "--type",
        required=True,
        choices=sorted(TYPE_RELIABILITY),
        help="Question type A-H, human-judgment call (pipeline/question_types.py) -- not inferred by this CLI",
    )
    p_query.add_argument("--answer", required=True, help="The answer's prose, rendered into block (B)")
    p_query.add_argument(
        "--claims",
        help="JSON ClaimSet payload (PROGRESS.md D1 schema): "
        '\'{"claims": [{"kind": "overdue_set", "control_ids": [...]}, ...]}\'',
    )
    p_query.add_argument("--claims-file", help="Path to a JSON file with the same shape as --claims")
    p_query.add_argument(
        "--reference-date",
        default=datetime.date.today().isoformat(),
        help="Date OVERDUE_SET claims are checked against (default: today, %(default)s)",
    )
    p_query.add_argument("--question-id", help="Override the derived question id (default: stable hash of the question text)")
    p_query.add_argument("--format", choices=["text", "json"], default="text", help="Output format (default: text)")
    p_query.set_defaults(func=cmd_query)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
