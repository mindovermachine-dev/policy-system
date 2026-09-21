# Run Report — <run-id>

<!-- Copy this per run. Fill every section. Unreachable/unknown = state "not tested", never blank. -->

- **Run id:** <YYYY-MM-DD-<A|B|A-B>>
- **Date:** <ISO>
- **Operator:** agent (`ps-pentest` skill)
- **Target instance:** <dedicated throwaway instance id / cluster>
- **Data provenance:** **synthetic, no PII** (assert — if not, ABORT the run per the blast-radius fence)
- **Scenarios run:** <A> / <B> / <A+B>
- **Prior run (for diff):** <run-id or "none — first run">

## 0. Precondition / run-validity (P0)

| Check | Result |
|-------|--------|
| Auth actually enforced in the instance? | <yes / no — if NO, run is VOID; surface it first> |
| `psService.localTestBypass.enabled`? | <must be false; if true, VOID> |
| Blast-radius fence held (no prod/real-secrets/real-PII touched)? | <yes / no → ABORT> |
| Synthetic data only? | <yes / no → ABORT> |

> If any precondition is unmet, this run is **void** and the violated precondition is the headline
> finding. Do not interpret "everything open" as findings under a void run.

## 1. Posture (driven by Scenario A; sub-signal B)

| | Score | Trend vs prior |
|---|-------|----------------|
| Scenario A (primary) | <0–100> | <▲/▪/▼> |
| Scenario B (authz-integrity sub-signal) | <0–100> | <▲/▪/▼> |

Open findings: critical <n> · high <n> · medium <n> · low <n> · info <n>

## 2. Findings

<!-- For each finding, copy the block. Findings OUTSIDE the threat-coverage-map are welcome: note them. -->

- **ID:** PS-####
  **Severity:** <critical/high/medium/low/info>
  **Scenario(s):** <A / B / A+B>
  **Surface:** <matches a threat-coverage-map surface, or "outside map: ...">
  **Phase found:** <P0–P5>
  **Title:** <one line>
  **Description:** <what, where, evidence>
  **Reproduction:** <exact, repeatable steps on the test instance>
  **Impact (in the blast radius):** <worst-case consequence>
  **Remediation → target-state:** <the "should"; decided post-hoc, NOT a pre-test bar>
  **Regression status:** <new / known (since <run-id>) / regressed / improved / still open>
  **Status:** open / mitigated / closed

## 3. Triangulation / out-of-coverage statement

- **Explicitly NOT tested / out of coverage:** <list — e.g. IdP internals, GitHub platform, real prod/PII>
- **Known blind spots:** <anything we could not reach; these are future work, not absolution>

## 4. Triage signal (3 booleans — NOT a pass/fail gate)

1. New critical/high since prior run? **<Y/N>** — <which>
2. Net regression (more/severer open than prior)? **<Y/N>** — <delta>
3. Coverage shrank or a phase skipped? **<Y/N>** — <what>

**Verdict:** <"needs attention" iff any true, else "clean run (still a prompt for human review, not
a certification)"> — one-line "why".

> After writing the report: the agent appends the run row to `posture-history.md` and the findings to
> `baseline.yaml` (`findings` + a `runs` entry). The agent does **not** commit — the user does.
