# © 2026 Cartman ApS. All rights reserved.
"""Validates Stage 3 (pipeline/routing.py) against the known target cases.

No FalkorDB dependency -- like Stage 1/2, routing is pure question-text +
type classification (Stage 4's actual checks are what touch the graph).

Two things this file has to prove, same bar as every other mechanism here:
1. Each type/signal combination routes to the path README specifies.
2. The one real ambiguity in README's own routing bullets -- what happens
   when a type-B/C/D question is ALSO Stage 2 multi_part -- resolves the
   way routing.py's module docstring commits to, using SEC-M4 (a real,
   already-shipped target case) as the concrete proof, not a synthetic one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.alias_table import run_stage1  # noqa: E402
from pipeline.routing import _is_hypothetical_chain, route_question  # noqa: E402
from pipeline.structural import run_stage2  # noqa: E402
from pipeline.types import MatchKind, RoutingPath, Stage1Result, Stage2Result, TermMatch  # noqa: E402

_SEC_M2_TEXT = "Which checks are overdue for review right now — not just due soon?"
_SEC_M4_TEXT = (
    "Which checks come due for review before the end of August 2026, "
    "and which are already overdue?"
)
_AU_M4_TEXT = (
    "Which GDPR articles currently have only stale requirement-to-control "
    "evidence chains, and why?"
)
_AU_H4_TEXT = (
    "If our log-retention check turns out to have failed, which "
    "regulatory requirements does that undermine?"
)
_AU_H2_TEXT = (
    "Trace the CRA's actively-exploited-vulnerability reporting duty from "
    "the regulation text all the way into our internal governance — does "
    "the trail reach a check that's actually running?"
)
_SEC_E1_TEXT = "Which Controls implement the incident/vulnerability response policy?"
_EM_E3_TEXT = "How many of our GDPR evidence chains would currently hold up in an audit?"
_SA_H2_TEXT = (
    "If a single capability of ours fails, which failure endangers the "
    "most obligations -- and is that even the right way to think about "
    "criticality?"
)


def _route(question_id: str, question_type: str, text: str):
    stage1 = run_stage1(question_id, text)
    stage2 = run_stage2(question_id, text)
    return route_question(stage1, stage2, question_type)


class TestRouting(unittest.TestCase):
    def test_undefined_term_refuses(self):
        stage1 = Stage1Result(
            question_id="SYNTH-UNDEFINED",
            question_text="What does the frobnicator obligation require?",
            term_matches=[TermMatch(surface_text="frobnicator", canonical_term=None, kind=MatchKind.NO_MATCH)],
        )
        stage2 = Stage2Result(question_id="SYNTH-UNDEFINED", question_text=stage1.question_text)
        decision = route_question(stage1, stage2, "B")
        self.assertEqual(decision.path, RoutingPath.REFUSE)
        self.assertIn("frobnicator", decision.reason)

    def test_type_g_decomposes(self):
        decision = _route("SA-H2", "G", _SA_H2_TEXT)
        self.assertEqual(decision.path, RoutingPath.DECOMPOSE)

    def test_type_f_decomposes(self):
        stage1 = Stage1Result(question_id="SYNTH-F", question_text="Are we compliant with GDPR Art 32?")
        stage2 = Stage2Result(question_id="SYNTH-F", question_text=stage1.question_text)
        decision = route_question(stage1, stage2, "F")
        self.assertEqual(decision.path, RoutingPath.DECOMPOSE)

    def test_alias_near_match_decomposes(self):
        # No live question currently trips MatchKind.ALIAS (every curated
        # term's alias list is empty today -- see alias_table.py). Testing
        # this branch needs a constructed Stage1Result, same honesty as
        # compose.py's own note that the undefined-term branch is
        # currently dead code in practice.
        stage1 = Stage1Result(
            question_id="SYNTH-ALIAS",
            question_text="Which controls have lapsed their review?",
            term_matches=[
                TermMatch(
                    surface_text="lapsed",
                    canonical_term="overdue",
                    kind=MatchKind.ALIAS,
                    definition="...",
                    disambiguation_required=True,
                )
            ],
        )
        stage2 = Stage2Result(question_id="SYNTH-ALIAS", question_text=stage1.question_text)
        decision = route_question(stage1, stage2, "B")
        self.assertEqual(decision.path, RoutingPath.DECOMPOSE)

    def test_type_a_e_h_direct_confident_no_mandatory_check(self):
        for question_type in ("A", "E", "H"):
            with self.subTest(question_type=question_type):
                stage1 = Stage1Result(question_id=f"SYNTH-{question_type}", question_text="What's the status of Control X?")
                stage2 = Stage2Result(question_id=f"SYNTH-{question_type}", question_text=stage1.question_text)
                decision = route_question(stage1, stage2, question_type)
                self.assertEqual(decision.path, RoutingPath.DIRECT_CONFIDENT)
                self.assertEqual(decision.mandatory_check_names, frozenset())

    def test_sec_m2_type_b_overdue_disambiguation_requires_rule_check(self):
        decision = _route("SEC-M2", "B", _SEC_M2_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(decision.mandatory_check_names, frozenset({"rule_overdue_excludes_deprecated"}))

    def test_au_m4_type_b_stale_disambiguation_requires_unbuilt_check(self):
        # The concrete gap this session closes: routing correctly names a
        # mandatory check that doesn't exist yet, rather than silently
        # requiring nothing (which is what a vacuous fitness-gate pass
        # looked like before Stage 3 existed).
        decision = _route("AU-M4", "B", _AU_M4_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(decision.mandatory_check_names, frozenset({"stale_chain_strict_reading"}))

    def test_sec_m4_multi_part_still_routes_direct_not_decompose(self):
        # The design resolution this module's docstring commits to: SEC-M4
        # is Stage 2 multi_part=True AND a validated type-B direct-answer
        # target case -- the type's own known trigger must win, not the
        # structural signal, or this would contradict an already-shipped,
        # already-validated Stage 4 mechanism (test_stage4.py).
        stage1 = run_stage1("SEC-M4", _SEC_M4_TEXT)
        stage2 = run_stage2("SEC-M4", _SEC_M4_TEXT)
        self.assertTrue(stage2.multi_part, "test premise: SEC-M4 must actually trip Stage 2's multi_part heuristic")
        decision = route_question(stage1, stage2, "B")
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(decision.mandatory_check_names, frozenset({"rule_overdue_excludes_deprecated"}))

    def test_sec_e1_type_b_no_disambiguation_requires_some_grounding_check(self):
        decision = _route("SEC-E1", "B", _SEC_E1_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(
            decision.mandatory_check_names,
            frozenset({"existence_grounding", "scope_match_regulation_routing", "fanout_maximum", "completeness_grounding"}),
        )

    def test_au_h4_type_d_hypothetical_chain_requires_grounding_check(self):
        decision = _route("AU-H4", "D", _AU_H4_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(
            decision.mandatory_check_names,
            frozenset({"existence_grounding", "scope_match_regulation_routing", "fanout_maximum", "completeness_grounding"}),
        )

    def test_au_h2_type_d_not_hypothetical_chain_no_mandatory_check(self):
        # Non-regression: AU-H2 is type D but not the "if X breaks" shape --
        # must not spuriously require the hypothetical-chain grounding set.
        decision = _route("AU-H2", "D", _AU_H2_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(decision.mandatory_check_names, frozenset())

    def test_em_e3_type_c_entity_type_recorded_requires_cross_check(self):
        decision = _route("EM-E3", "C", _EM_E3_TEXT)
        self.assertEqual(decision.path, RoutingPath.DIRECT_MANDATORY_CHECK)
        self.assertEqual(decision.mandatory_check_names, frozenset({"entity_type_cross_check"}))

    def test_hypothetical_chain_detection_broadened_set(self):
        # Overfitting audit finding (PROGRESS.md): the original regex was
        # built from AU-H4/SEC-H4's exact wording and missed paraphrases.
        # Broadened to two independent signals (conditional marker +
        # failure/state verb); validated here against a wider set than the
        # two cases it was built from, including the specific paraphrases
        # that were confirmed live to slip past the old version.
        must_match = [
            _AU_H4_TEXT,  # "If our log-retention check turns out to have failed..."
            (
                "If the Encryption-at-Rest check fails its review on August "
                "15, which regulatory duties does that put at risk?"
            ),  # SEC-H4
            "Should the vulnerability-scanning check stop working, what obligations are at risk?",
            "Assuming the incident-response control is broken, what duties are exposed?",
            "What happens to our GDPR posture if the DPO capability collapses?",
            "If access control authentication were to fail tomorrow, what would we be exposed to?",
        ]
        must_not_match = [
            _AU_H2_TEXT,  # real target case: no conditional/failure framing at all
            # Real-time state question, not hypothetical -- no conditional
            # marker despite containing a failure-state word.
            "Is the vulnerability-management capability currently failing any obligations?",
            _SEC_E1_TEXT,
        ]
        for text in must_match:
            with self.subTest(text=text):
                self.assertTrue(_is_hypothetical_chain(text), f"expected match: {text!r}")
        for text in must_not_match:
            with self.subTest(text=text):
                self.assertFalse(_is_hypothetical_chain(text), f"expected no match: {text!r}")

    def test_unknown_type_raises(self):
        stage1 = Stage1Result(question_id="SYNTH-Z", question_text="text")
        stage2 = Stage2Result(question_id="SYNTH-Z", question_text="text")
        with self.assertRaises(ValueError):
            route_question(stage1, stage2, "Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
