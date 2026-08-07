#!/usr/bin/env python3
"""Entity-resolver bake-off: lexical substring vs TF-IDF cosine, against the
known free-text -> Capability cases from ../query1/golden-answers.md (H3, H6,
H8, H9, H11, M3). Per q-approach4.md §10.5 / mining-pass.md's "next steps":
run this before committing to any embeddings pipeline, on real cases with a
known-correct answer, not a hypothetical.

Each case is tested with several real phrasings a resolver might actually
receive -- the bare acronym, its expansion, and the full scenario sentence
from the question catalog -- because a resolver's practical performance
depends heavily on which of those the calling system passes in, not just on
the resolution strategy itself.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from falkordb import FalkorDB  # noqa: E402

from catalog import compile_catalog  # noqa: E402
from resolver import LexicalResolver, TfidfResolver  # noqa: E402

CASES = [
    ("H11", ["MFA", "multi-factor authentication", "missing MFA control"],
     {"cap_access_control_authentication_151816"}),
    ("H9", ["rate limiting", "rate-limiting", "throttling", "missing rate-limiting on an endpoint"],
     set()),  # golden: no real hit -- graph doesn't model this at all
    ("H6", ["SBOM", "Software Bill of Materials", "software bill of materials capability"],
     {"cap_component_inventory_sbom_management_b5223c"}),
    ("H3-logs", ["logs access", "logs access to data"],
     {"cap_access_control_authentication_151816"}),
    ("H3-encrypt", ["doesn't encrypt data at rest", "encryption at rest", "data encryption"],
     {"cap_data_encryption_0e50d3"}),
    ("H8", ["stores customer PII", "PII", "personal data storage compliance"],
     {
         "cap_data_encryption_0e50d3",
         "cap_access_control_authentication_151816",
         "cap_security_logging_c4d9e2",
         "cap_data_protection_impact_assessment_a51acb",
         "cap_secure_data_removal_portability_3d7885",
     }),
    ("M3", ["Security Logging-type capability", "security logging"],
     {"cap_security_logging_c4d9e2"}),
]


def main() -> None:
    db = FalkorDB(host="localhost", port=6379)
    g = db.select_graph("policy_system")
    cat = compile_catalog(g)
    lex = LexicalResolver(cat.all_capabilities)
    tfidf = TfidfResolver(cat.all_capabilities)

    print(f"{'case':<12} {'phrasing':<45} {'lexical hit?':<14} {'tfidf hit?':<14}")
    print("-" * 90)
    lex_scorecard: dict[str, list[bool]] = {}
    tfidf_scorecard: dict[str, list[bool]] = {}
    for case, phrasings, golden_ids in CASES:
        lex_scorecard.setdefault(case, [])
        tfidf_scorecard.setdefault(case, [])
        for phrasing in phrasings:
            lex_hits = {h.capability_id for h in lex.resolve(phrasing, top_k=5)}
            tfidf_hits = {h.capability_id for h in tfidf.resolve(phrasing, top_k=5)}

            if golden_ids:
                lex_ok = bool(lex_hits & golden_ids)
                tfidf_ok = bool(tfidf_hits & golden_ids)
            else:
                # H9's golden is "no real hit" -- correct behavior is an
                # EMPTY result, a hit here is a false positive, not a catch.
                lex_ok = not lex_hits
                tfidf_ok = not tfidf_hits

            lex_scorecard[case].append(lex_ok)
            tfidf_scorecard[case].append(tfidf_ok)
            print(f"{case:<12} {phrasing:<45} {'OK' if lex_ok else 'MISS':<14} {'OK' if tfidf_ok else 'MISS':<14}")

    print("\nPer-case summary (OK on ANY phrasing counts as resolvable that way):")
    for case, _, golden_ids in CASES:
        lex_any = any(lex_scorecard[case])
        tfidf_any = any(tfidf_scorecard[case])
        lex_all = all(lex_scorecard[case])
        tfidf_all = all(tfidf_scorecard[case])
        print(f"  {case:<12} lexical: any={lex_any} all={lex_all}   tfidf: any={tfidf_any} all={tfidf_all}")


if __name__ == "__main__":
    main()
