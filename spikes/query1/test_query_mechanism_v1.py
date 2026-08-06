#!/usr/bin/env python3
"""Golden-value test harness for query_mechanism_v1.py.

Runs every in-scope question from example-questions.md through the router
and diffs the result against the golden values computed in golden-answers.md.
Out-of-scope (rubric-graded) questions are checked for a clean
NO_TEMPLATE_MATCH instead of a wrong answer -- for this mechanism, that's a
pass, not a failure.
"""

from query_mechanism_v1 import QueryMechanismV1, NoTemplateMatch

# Each case: (id, question, expected -- a set of tuples for set-match,
# or None for cases checked by a custom `check` function instead).
CASES = [
    ("S1", "What roles does GDPR define?",
     {("Controller",), ("Data Protection Officer",), ("Joint controller",), ("Processor",), ("Representative",)}),
    ("S2", "What's the text of CRA Article 13.1?",
     {("CRA-1.0_req_art_13.1",
       "Manufacturers shall ensure that a product with digital elements is designed, developed and produced in accordance with the essential cybersecurity requirements set out in Part I of Annex I.")}),
    ("S3", "What obligations does the Manufacturer role carry under CRA?",
     None),  # checked by count below (48)
    ("S4", "What capabilities does 'Maintain Security Logging' require?",
     {("cap_security_logging_c4d9e2", "Security Logging")}),
    ("S5", "When does CRA become effective, and what's its current status?",
     {("CRA-1.0", "2027-12-11", "active")}),
    ("S6", "Which requirement does the 'Maintain Security Logging' obligation satisfy?",
     {("CRA-1.0_req_annex1_pt1_2l",
       "Products shall provide security-related information by recording and monitoring relevant internal activity, including access to or modification of data, services or functions, with a user opt-out mechanism.")}),
    ("S7", "What policy governs the 'Security Logging' capability?",
     {("pol_data_protection_security_policy_8e4c18", "Data Protection & Security Policy", "approved")}),
    ("S8", "List the standards under the Data Protection Policy.",
     {("std_pol_data_protection_security_policy_8e4c18_v1", "Encryption-at-Rest & In-Transit Standard", "implemented"),
      ("std_pol_data_protection_security_policy_8e4c18_v2", "Access Control, MFA & Session Standard", "implemented"),
      ("std_pol_data_protection_security_policy_8e4c18_v3", "Security Log Retention & SIEM Standard", "reviewed")}),
    ("M1", "Which capabilities are required by more than one obligation?",
     None),  # checked by count below (52)
    ("M2", "Trace the full path from CRA Art. 13.1 to whatever capability it ultimately requires.",
     {("CRA-1.0_req_art_13.1",
       "Manufacturers shall ensure that a product with digital elements is designed, developed and produced in accordance with the essential cybersecurity requirements set out in Part I of Annex I.",
       "obl_ensure_secure_product_design_and_development_c56d3c", "Ensure Secure Product Design and Development",
       "cap_secure_development_lifecycle_9f3224", "Secure Development Lifecycle")}),
    ("M4", "How many obligations does GDPR place on Data Processors vs. Data Controllers?",
     {("Controller", 148), ("Processor", 55)}),
    ("M6", "Which obligations are backed by the weakest extraction confidence?",
     None),  # checked by count below (24, threshold 0.80)
    ("M7", "Show every path from a GDPR requirement down to a Control that verifies it.",
     None),  # checked by count + trust-flag split below
    ("M8", "Which capabilities does our internal Helvex regulation share with CRA?",
     {("cap_security_logging_c4d9e2", "Security Logging")}),
    ("H2", "Which capabilities required by CRA have no governing Policy yet?",
     None),  # checked by count below (55)
    ("H4", "Show me the audit evidence that our log retention control passed last quarter.",
     {("ctrl_std_pol_data_protection_security_policy_8e4c18_v3_automated",
       "evidence://ci/log-retention-check/latest", "implemented", "2026-11-01")}),
    ("H7", "Which of our automated controls are due for review in the next 30 days?",
     {("ctrl_std_pol_data_protection_security_policy_8e4c18_v1_automated",
       "Automated Encryption-at-Rest Compliance Check", "implemented", "2026-08-15"),
      ("ctrl_std_pol_data_protection_security_policy_8e4c18_v2_automated",
       "Access Control & MFA Enforcement Audit", "implemented", "2026-08-25")}),
]

# Rubric-graded, no template should exist for these -- NO_TEMPLATE_MATCH is
# the correct behavior here, not a gap.
OUT_OF_SCOPE = [
    ("M3", "Which obligations, across all three loaded regulations, require a 'Security Logging'-type capability?"),
    ("M5", "Do CRA and NIS2 impose obligations on similar roles (e.g. something Manufacturer-like)?"),
    ("H1", "Are we compliant with GDPR Article 32?"),
    ("H3", "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?"),
    ("H5", "NIS2 was updated - which of our Policies are now potentially out of date?"),
    ("H6", "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy?"),
]


def project(rows, columns, keep):
    idx = [columns.index(k) for k in keep]
    return {tuple(row[i] for i in idx) for row in rows}


def main() -> int:
    mech = QueryMechanismV1()
    passed, failed = 0, 0

    print("=== In-scope questions (exact/set-match against golden-answers.md) ===\n")
    for qid, question, expected in CASES:
        try:
            result = mech.ask(question)
        except NoTemplateMatch as e:
            print(f"[FAIL] {qid}: expected a match, got {e}")
            failed += 1
            continue

        ok = None
        note = ""
        if qid == "S3":
            ok = len(result.rows) == 48
            note = f"({len(result.rows)} obligations, expected 48)"
        elif qid == "M1":
            ok = len(result.rows) == 52
            note = f"({len(result.rows)} capabilities, expected 52)"
        elif qid == "M6":
            ok = len(result.rows) == 24
            note = f"({len(result.rows)} obligations <= 0.80, expected 24)"
        elif qid == "M7":
            trusted = sum(1 for r in result.rows if r[result.columns.index("is_current_evidence")])
            stale = len(result.rows) - trusted
            ok = len(result.rows) == 57 and stale == 26
            note = f"({len(result.rows)} chains, expected 57; {trusted} current / {stale} stale, expected 26 stale)"
        elif qid == "H2":
            ok = len(result.rows) == 55
            note = f"({len(result.rows)} ungoverned, expected 55)"
        else:
            got = project(result.rows, result.columns, result.columns) if expected else set()
            got_full = {tuple(row) for row in result.rows}
            ok = got_full == expected
            note = "" if ok else f"got {got_full}, expected {expected}"

        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {qid} (template={result.template}) {note}")
        passed += ok
        failed += not ok

    print("\n=== Out-of-scope questions (must be an honest NO_TEMPLATE_MATCH) ===\n")
    for qid, question in OUT_OF_SCOPE:
        try:
            result = mech.ask(question)
            print(f"[FAIL] {qid}: expected NO_TEMPLATE_MATCH, got template={result.template} rows={result.rows[:2]}...")
            failed += 1
        except NoTemplateMatch:
            print(f"[PASS] {qid}: correctly refused (no template)")
            passed += 1

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
